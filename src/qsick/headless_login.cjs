'use strict';
// Shared automatic/manual quick-login controller. No QR generation or credential storage.
class LoginError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}
function createQuickLogin({ getLogin, getState, getLastAccount, initialize, publish, timeoutMs = 20000 }) {
  let busy = false, revision = 0, cancelCurrent = null;
  function invalidate() {
    revision++;
    if (cancelCurrent) cancelCurrent(new LoginError('SUPERSEDED', '登录尝试已被新的登录流程替代'));
  }
  async function attempt() {
    const state = getState();
    if (state.phase === 'ready') return { accepted: false, already_logged_in: true, phase: 'ready', uin: state.uin };
    if (busy || state.phase === 'initializing') throw new LoginError('BUSY', '正在登录或初始化会话，请稍候');
    const last = getLastAccount();
    if (!last || !/^[1-9]\d*$/.test(String(last.uin || '')) || !/^u_[A-Za-z0-9_-]+$/.test(String(last.uid || ''))) {
      throw new LoginError('NO_HISTORY', '该实例没有可用的上次登录记录，请先扫码登录');
    }
    const login = getLogin();
    if (!login || typeof login.quickLoginWithUin !== 'function') throw new LoginError('NOT_READY', '登录服务尚未就绪，请稍后重试');
    busy = true; const current = ++revision;
    // Everything after this point must run inside try/finally: an early return
    // (no ticket, network timeout) used to leave busy stuck true forever, which
    // is why the panel kept reporting "正在登录或初始化会话" on every retry.
    let timer;
    try {
      await waitForNetwork(login, current);
      if (revision !== current) throw new LoginError('SUPERSEDED', '登录尝试已失效');
      const ticket = await hasTicket(login, String(last.uin));
      if (revision !== current) throw new LoginError('SUPERSEDED', '登录尝试已失效');
      if (ticket === false) {
        throw new LoginError('NO_TICKET', 'QQ 没有保存该账号的登录票据（本地登录列表为空或缺少该账号），只能扫码登录');
      }
      publish({ phase: 'quick_login', uin: String(last.uin), uid: String(last.uid), error: '' });
      const result = await Promise.race([
        Promise.resolve().then(() => login.quickLoginWithUin(String(last.uin))),
        new Promise((_, reject) => {
          cancelCurrent = reject;
          timer = setTimeout(() => { revision++; reject(new LoginError('TIMEOUT', '快捷登录超时，请稍后重试或扫码登录')); }, timeoutMs);
        })
      ]);
      if (revision !== current) throw new LoginError('SUPERSEDED', '登录尝试已失效');
      // NapCat treats a present errMsg as failure even when result is "0".
      const errText = result && result.loginErrorInfo && result.loginErrorInfo.errMsg;
      if (!result || (result.result !== 0 && result.result !== '0') || errText) {
        throw new LoginError('LOGIN_REJECTED', errText || 'QQ 未接受快捷登录，可能需要重新扫码验证');
      }
      const returnedUin = result.uin ?? result.loginInfo?.uin;
      if (returnedUin != null && String(returnedUin) !== String(last.uin)) throw new LoginError('ACCOUNT_MISMATCH', 'QQ 返回的账号与上次登录账号不一致');
      publish({ phase: 'initializing', uin: String(last.uin), uid: String(last.uid), error: '' });
      await initialize(String(last.uin), String(last.uid));
      return { accepted: true, already_logged_in: false, phase: getState().phase, uin: String(last.uin) };
    } catch (error) {
      if (error.code !== 'SUPERSEDED' && (revision === current || error.code === 'TIMEOUT')) {
        publish({ phase: 'login_failed', error: String(error.message || error) });
      }
      throw error;
    } finally { clearTimeout(timer); cancelCurrent = null; busy = false; }
  }
  /**
   * Confirm the account really has a reusable ticket before dialling.
   *
   * QQ only accepts quick login for accounts present in its local login list;
   * calling quickLoginWithUin blindly is what produced "登录态已失效" every time.
   */
  async function hasTicket(login, uin) {
    if (typeof login.getLoginList !== 'function') return null; // unknown, let QQ decide
    let list;
    try { list = await login.getLoginList(); }
    catch (_) { return null; }
    const items = list && list.LocalLoginInfoList;
    if (!Array.isArray(items)) return null;
    if (!items.length) return false;
    return items.some(item => String(item && item.uin) === String(uin));
  }

  async function waitForNetwork(login, current) {
    if (revision !== current) throw new LoginError('SUPERSEDED', '登录尝试已失效');
    if (typeof login.getMsfStatus !== 'function') return;
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {
      if (revision !== current) throw new LoginError('SUPERSEDED', '登录尝试已失效');
      let status;
      try { status = login.getMsfStatus(); } catch (_) { return; }
      // 3 means the login service is still waiting for a usable connection.
      if (status !== 3) return;
      await new Promise(resolve => setTimeout(resolve, 300));
    }
    throw new LoginError('TIMEOUT', '网络连接尚未就绪，快捷登录已取消');
  }

  return { attempt, invalidate, isBusy: () => busy, hasTicket };
}
module.exports = { createQuickLogin, LoginError };
