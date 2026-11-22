'use strict';
// Offline media tests: real temp files, stubbed native services, no QQ, no network.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const zlib = require('node:zlib');
const { createMediaPreparer, imageSize, stripFileUri } = require('../src/qsick/headless_media.cjs');
const codec = require('../src/qsick/headless_codec.cjs');

let root;
test.before(async () => { root = await fsp.mkdtemp(path.join(os.tmpdir(), 'qsick-media-test-')); });
test.after(async () => { await fsp.rm(root, { recursive: true, force: true }); });

/** Minimal but structurally valid PNG of the requested size. */
function png(width, height) {
  const crcTable = Array.from({ length: 256 }, (_, n) => {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    return c >>> 0;
  });
  const crc = buf => {
    let c = 0xffffffff;
    for (const byte of buf) c = crcTable[(c ^ byte) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  };
  const chunk = (type, data) => {
    const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
    const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
    const sum = Buffer.alloc(4); sum.writeUInt32BE(crc(body));
    return Buffer.concat([len, body, sum]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0); ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; ihdr[9] = 2; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;
  const raw = Buffer.alloc((width * 3 + 1) * height);
  const idat = zlib.deflateSync(raw);
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr), chunk('IDAT', idat), chunk('IEND', Buffer.alloc(0))]);
}

function jpeg(width, height) {
  return Buffer.concat([
    Buffer.from([0xff, 0xd8]),
    Buffer.from([0xff, 0xe0, 0x00, 0x04, 0x00, 0x00]),
    Buffer.from([0xff, 0xc0, 0x00, 0x11, 0x08,
      (height >> 8) & 0xff, height & 0xff, (width >> 8) & 0xff, width & 0xff,
      0x03, 0x01, 0x11, 0x00, 0x02, 0x11, 0x01, 0x03, 0x11, 0x01]),
    Buffer.from([0xff, 0xd9]),
  ]);
}

function gif(width, height) {
  const buf = Buffer.alloc(20);
  buf.write('GIF89a', 0, 'ascii');
  buf.writeUInt16LE(width, 6); buf.writeUInt16LE(height, 8);
  return buf;
}

function fixture(overrides = {}) {
  const staged = [];
  const service = {
    getRichMediaFilePathForGuild(arg) {
      staged.push(arg);
      return path.join(root, 'qq-cache', arg.md5HexStr + '-' + arg.fileName);
    },
    ...overrides,
  };
  const session = { getMsgService: () => service };
  return { preparer: createMediaPreparer({ getSession: () => session }), staged, service };
}

test('reads PNG, JPEG and GIF dimensions from real headers', async () => {
  const cases = [
    ['a.png', png(1920, 1080), 1920, 1080],
    ['b.jpg', jpeg(800, 600), 800, 600],
    ['c.gif', gif(64, 32), 64, 32],
  ];
  for (const [name, bytes, width, height] of cases) {
    const file = path.join(root, name);
    await fsp.writeFile(file, bytes);
    assert.deepEqual(imageSize(await fsp.readFile(file)), { width, height }, name);
  }
  assert.equal(imageSize(Buffer.from('not an image at all')), null);
});

test('image preparation stages a copy and builds the verified picElement', async () => {
  const file = path.join(root, 'photo.png');
  await fsp.writeFile(file, png(120, 90));
  const { preparer, staged } = fixture();
  const element = await preparer.prepareImage({ path: file }, { peer: { chatType: 2 } }, 0);
  assert.equal(element.elementType, 2);
  assert.equal(element.picElement.picWidth, 120);
  assert.equal(element.picElement.picHeight, 90);
  assert.equal(element.picElement.original, true);
  assert.equal(element.picElement.picSubType, 0);
  assert.match(element.picElement.md5HexStr, /^[0-9a-f]{32}$/);
  assert.equal(element.picElement.fileSize, String(png(120, 90).length));
  assert.equal(element.picElement.picType, 1000);
  assert.ok(staged[0].needCreate === true && staged[0].elementType === 2);
  assert.ok(fs.existsSync(element.picElement.sourcePath), 'staged copy should exist');
  await preparer.release();
  assert.equal(fs.existsSync(element.picElement.sourcePath), false, 'release removes the staged copy');
});

test('gif images use the gif picture type', async () => {
  const file = path.join(root, 'anim.gif');
  await fsp.writeFile(file, gif(10, 10));
  const { preparer } = fixture();
  assert.equal((await preparer.prepareImage({ path: file }, {}, 0)).picElement.picType, 2000);
});

test('a sticker keeps picSubType 1 through staging and the picElement', async () => {
  // 收藏表情必须带着 picSubType=1 发出，否则接收端只会渲染成一张图片。
  // 两个字段名都要认：OneBot 段用 sub_type，WebUI 上传表单用 pic_sub_type。
  const file = path.join(root, 'fav.gif');
  await fsp.writeFile(file, gif(64, 64));
  for (const data of [{ path: file, sub_type: 1 }, { path: file, pic_sub_type: 1 }]) {
    const { preparer, staged } = fixture();
    const element = await preparer.prepareImage(data, { peer: { chatType: 2 } }, 0);
    assert.equal(element.picElement.picSubType, 1, JSON.stringify(data));
    // 富媒体暂存也要按"表情"分目录，否则 QQ 仍按普通图片归类。
    assert.equal(staged[staged.length - 1].elementSubType, 1);
    await preparer.release();
  }
  const plain = fixture();
  assert.equal((await plain.preparer.prepareImage({ path: file }, {}, 0)).picElement.picSubType, 0);
  await plain.preparer.release();
});

test('group files are staged, private files keep their original path', async () => {
  const file = path.join(root, 'doc.txt');
  await fsp.writeFile(file, 'hello');
  const { preparer } = fixture();
  const group = await preparer.prepareFile({ path: file, name: 'doc.txt' }, { peer: { chatType: 2 } }, 0);
  assert.equal(group.elementType, 3);
  assert.equal(group.fileElement.fileName, 'doc.txt');
  assert.equal(group.fileElement.fileSize, '5');
  assert.notEqual(group.fileElement.filePath, file);
  const priv = await preparer.prepareFile({ path: file }, { peer: { chatType: 1 } }, 0);
  assert.equal(priv.fileElement.filePath, file);
  await preparer.release();
});

test('media rejects missing, empty, oversized and non-file references', async () => {
  const { preparer } = fixture();
  await assert.rejects(preparer.prepareImage({ path: path.join(root, 'nope.png') }, {}, 0), e => e.code === 'NOT_FOUND');
  const empty = path.join(root, 'empty.png');
  await fsp.writeFile(empty, '');
  await assert.rejects(preparer.prepareImage({ path: empty }, {}, 0), e => e.code === 'INVALID_PARAMS');
  await assert.rejects(preparer.prepareImage({ path: root }, {}, 0), e => e.code === 'INVALID_PARAMS');
  await assert.rejects(preparer.prepareImage({}, {}, 0), e => e.code === 'INVALID_PARAMS');
  const big = path.join(root, 'big.bin');
  await fsp.writeFile(big, Buffer.alloc(2048));
  const small = createMediaPreparer({ getSession: () => ({ getMsgService: () => ({}) }), maxBytes: 1024 });
  await assert.rejects(small.prepareFile({ path: big }, {}, 0), e => e.code === 'INVALID_PARAMS');
});

test('base64 media is written to a temporary file and cleaned up', async () => {
  const { preparer } = fixture();
  const payload = 'base64://' + png(4, 4).toString('base64');
  const element = await preparer.prepareImage({ base64: payload, name: 'inline.png' }, {}, 0);
  assert.equal(element.picElement.picWidth, 4);
  assert.ok(fs.existsSync(element.picElement.sourcePath));
  await preparer.release();
  assert.equal(fs.existsSync(element.picElement.sourcePath), false);
});

test('file:// URIs resolve to local paths', () => {
  assert.equal(stripFileUri('file:///C:/a/b.png'), 'C:/a/b.png');
  assert.equal(stripFileUri('/tmp/a.png'), '/tmp/a.png');
});

test('codec delegates image/file to preparers and refuses without one', async () => {
  const file = path.join(root, 'codec.png');
  await fsp.writeFile(file, png(20, 20));
  const { preparer } = fixture();
  const built = await codec.buildSendElements(
    [{ type: 'text', data: { text: 'look' } }, { type: 'image', data: { path: file } }],
    { prepareImage: preparer.prepareImage, prepareFile: preparer.prepareFile, peer: { chatType: 2, peerUid: '1' } },
  );
  assert.equal(built.length, 2);
  assert.equal(built[0].elementType, 1);
  assert.equal(built[1].elementType, 2);
  await preparer.release();
  await assert.rejects(
    codec.buildSendElements([{ type: 'image', data: { path: file } }], { peer: { chatType: 2, peerUid: '1' } }),
    e => e.code === 'UNSUPPORTED_ELEMENT',
  );
});


test('received media downloads on a correlated completion event', async () => {
  let listener;
  const calls = [];
  const service = {
    addKernelMsgListener(l) { listener = l; return 3; },
    removeKernelMsgListener(handle) { calls.push(['remove', handle]); },
    downloadRichMedia(arg) { calls.push(['download', arg]); },
  };
  const preparer = createMediaPreparer({ getSession: () => ({ getMsgService: () => service }) });
  const pending = preparer.downloadReceived({ peer: { chatType: 2, peerUid: '456' }, msgId: '777' }, { id: 'el9' });
  await new Promise(r => setImmediate(r));
  listener.onRichMediaDownloadComplete({ msgId: '1', msgElementId: 'el9', filePath: '/wrong.png' });
  listener.onRichMediaDownloadComplete({ msgId: '777', msgElementId: 'el9', fileErrCode: 0, filePath: '/tmp/ok.png' });
  assert.equal(await pending, '/tmp/ok.png');
  assert.deepEqual(calls[0][1].elementId, 'el9');
  assert.equal(calls[0][1].msgId, '777');
  assert.deepEqual(calls[calls.length - 1], ['remove', 3]);
});

test('received media reports failure as no path, never a guess', async () => {
  let listener;
  const service = {
    addKernelMsgListener(l) { listener = l; return 1; },
    removeKernelMsgListener() {},
    downloadRichMedia() {},
  };
  const preparer = createMediaPreparer({ getSession: () => ({ getMsgService: () => service }) });
  const pending = preparer.downloadReceived({ peer: { chatType: 2, peerUid: '1' }, msgId: '5' }, { id: 'e' });
  await new Promise(r => setImmediate(r));
  listener.onRichMediaDownloadComplete({ msgId: '5', msgElementId: 'e', fileErrCode: 4, fileErrMsg: 'gone' });
  assert.equal(await pending, null);
  assert.equal(await preparer.downloadReceived({}, { id: 'e' }), null);
  assert.equal(await preparer.downloadReceived({ peer: { chatType: 2, peerUid: '1' }, msgId: '5' }, {}), null);
});
