"""Read-only static source audit; writes only new docs/onebot-action-audit.json.
No project imports, native execution, runtime calls, tests, network, or config writes.
"""
import ast
import collections
import hashlib
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = ROOT / 'src/qsick/onebot/actions'
STANDARD = set('''send_private_msg send_group_msg send_msg delete_msg get_msg get_forward_msg send_like set_group_kick set_group_ban set_group_anonymous_ban set_group_whole_ban set_group_admin set_group_anonymous set_group_card set_group_name set_group_leave set_group_special_title set_friend_add_request set_group_add_request get_login_info get_stranger_info get_friend_list get_group_info get_group_list get_group_member_info get_group_member_list get_group_honor_info get_cookies get_csrf_token get_credentials get_record get_image can_send_image can_send_record get_status get_version_info set_restart clean_cache'''.split())
sources = {}
def text(path):
    raw = path.read_bytes()
    sources[path.relative_to(ROOT).as_posix()] = hashlib.sha256(raw).hexdigest()
    return raw.decode('utf-8-sig')
cat = json.loads(text(BASE / 'actions.json'))
js = text(ROOT / 'src/qsick/headless_actions.cjs')
def block(name):
    return re.search(r'const ' + name + r' = Object.freeze\(\{(.*?)\n\}\);', js, re.S).group(1)
keypattern = r"(?:'([^']+)'|([A-Za-z_][\w]*))\s*:\s*'([^']*)'"
aliases = {(a or b): c for a,b,c in re.findall(keypattern, block('ALIASES'))}
unresolved = {(a or b): c for a,b,c in re.findall(keypattern, block('UNRESOLVED'))}
requirements = {op: re.findall(r"'([^']+)'", vals) for op,vals in re.findall(r"'([^']+)'\s*:\s*\[([^]]*)\]", block('REQUIREMENTS'))}
requirements.update({
 'login.info': ['current verified login identity'],
 'friend.list': ['Buddy.getBuddyListFromCache', 'Profile.getCoreAndBaseInfo', 'initialized buddy/profile cache'],
 'friend.categories': ['Buddy.getBuddyListFromCache', 'Profile.getCoreAndBaseInfo', 'initialized category metadata'],
 'friend.statuses': ['Buddy.getBuddyListFromCache', 'Buddy.hasBuddyList', 'Profile.getCoreAndBaseInfo', 'all status facts'],
 'friend.relations': ['Buddy.getBuddyListFromCache', 'Buddy.hasBuddyList', 'Profile.getCoreAndBaseInfo', 'all relationship flags'],
 'message.send': ['Msg.sendMsg', 'Msg.generateMsgUniqueId', 'MSF.getServerTime', 'correlated native message receipt', 'verified UID resolution for private peers and user mentions'],
})
mock_tree = ast.parse(text(ROOT / 'src/qsick/transport/mock.py'))
mock_ops = {}
for n in mock_tree.body:
    if isinstance(n, ast.Assign) and any(isinstance(t,ast.Name) and t.id == '_OPS' for t in n.targets):
        mock_ops = {ast.literal_eval(k): ast.unparse(v) for k,v in zip(n.value.keys,n.value.values)}
for p in ['src/qsick/runtime.py','src/qsick/onebot/context.py','src/qsick/onebot/registry.py','src/qsick/onebot/actions/base.py','src/qsick/onebot/actions/loader.py','src/qsick/protocol/rkey.py','src/qsick/headless_sender.cjs','src/qsick/headless_codec.cjs','src/qsick/headless_host.js','src/qsick/transport/headless.py','native/qsick-payload/src/payload.cpp','tools/gen_payload_ops.py']:
    text(ROOT/p)
notes = {
 'can_send_image': ['Returns false in headless; true for any other transport, including unsupported pipe scaffold. Not native feature detection.'],
 'can_send_record': ['Returns false in headless; true for any other transport, including unsupported pipe scaffold. Not native feature detection.'],
 'get_status': ['Uses local login state only; good=logged_in does not verify health of every module or native capability.'],
 'get_login_info': ['Uses local identity only when logged_in and nonzero UIN; otherwise calls native login.info and propagates failure; rejects nonpositive returned UIN. Requires an established login identity.'], 
 'get_msg': ['Local stored-message lookup works only for observed/stored records; missing records fall through to unsupported message.get in headless.'],
 'get_rkey': ['RKeyManager.refresh suppresses failed rkey transport response; API can return successful [] or old cached tokens. No working headless rkey fetch.'],
 'get_group_member_info': ['Python member_json drops standard fields sex, age, area, unfriendly, title, title_expire_time, card_changeable; native data alone is not full OneBot compliance.'],
 'get_group_member_list': ['Python member_json drops standard fields sex, age, area, unfriendly, title, title_expire_time, card_changeable. no_cache extra parameter and get_group_member_list_no_cache alias do not force refresh.'],
 'get_friend_list': ['Core TTL cache; adapter discards no_cache extras. Native buddy list is cache-backed and refuses force refresh.'],
 'get_group_list': ['Core TTL cache; adapter discards no_cache extras. Native requires zero-status ACK plus full-list event.'],
 'get_group_info': ['Native requires zero-status ACK and matching detail event; no_cache is type-checked but not forwarded as a native refresh flag.'],
 'get_stranger_info': ['getUserDetailInfoByUin result and identity checked; no_cache is type-checked but not used as native refresh flag.'],
 'get_forward_msg': ['Adapter now returns standard message node-segment array plus legacy messages list. Still no headless forward.get handler; schema repair alone does not supply native retrieval.'],
 'get_record': ['Ignores standard out_format; does not convert/download audio; returns original identifier as file plus URL.'],
 'get_image': ['Does not download image; returns original identifier as file plus URL, not required downloaded local path.'],
 'get_cookies': ['Ignores standard domain parameter; absent cookies default to empty string in successful upstream payload.'],
 'get_credentials': ['Ignores standard domain parameter; absent cookies/token fields default to empty string/zero.'],
 'get_csrf_token': ['Calls cookies op; absent csrf_token defaults to zero in successful upstream payload.'],
 'set_group_add_request': ['Does not implement standard type alias for sub_type. No verified native request handler.'],
 'set_restart': ['Forwards runtime.restart; runtime core does not handle it. Ignores standard delay and does not return asynchronous restart status. Mock generic success is not a restart.'],
 'clean_cache': ['Forwards cache.clean; does not clear core TTLCache, media files, or message store locally. Headless rejects; mock generic success does not clean anything.'],
 'set_group_special_title': ['Python now forwards canonical group.special_title; native explicitly rejects because no verified title-setting service signature.'],
 'set_group_leave': ['Only leave supported; is_dismiss=true is explicitly rejected because destroyGroup completion is unverified.'],
 'set_group_card': ['One writer call only; desired state must be seen in forced complete member readback; if already matching, verified no-write success is intentional.'],
 'set_group_admin': ['One writer call only; role must be confirmed by forced complete member readback; owner changes rejected.'],
 'set_group_ban': ['Requires verified cached UIN-to-UID mapping; never fabricates UID; zero-status native ACK required.'],
 'delete_msg': ['Requires exact native message reference retained in host memory for that message; zero-status recall ACK required.'],
 'get_group_admin_list': ['Complete unique member result; role=admin only unless include_owner=true. Query, not a management operation.'],
 'get_group_member_count': ['Counts complete unique member records; not maximum capacity or cached metadata count.'],
 'get_group_share_link': ['Query only: zero-status ACK plus nonempty native URL; does not send a card or open URL.'],
 'get_group_share_ark': ['Query only: zero-status ACK plus JSON-object arkJson; does not send a card.'],
 'get_friend_status_list': ['Complete initialized cache required; every status field typed and identity-checked; numeric status codes are not mapped to online booleans; no forced refresh.'],
 'get_friend_relation_list': ['Complete initialized cache required; every relationship flag typed; preserves native isBlock/isBlocked distinction; no forced refresh.'],
 'nc_get_user_status': ['Verified cached UID and non-null profile status required; no forced refresh or inferred online/offline semantics.'],
 'get_group_shut_list': ['Zero-status ACK plus matching mute-list event; no fabricated empty success.'],
 'get_friends_with_category': ['Adapter forwards empty params, discarding no_cache; native cache category metadata required.'],
}
for name in ('send_msg','send_group_msg','send_private_msg'):
    notes[name] = ['Partial segment support only: text, at (group only), legacy face 0..221, json, reply. Image/record/video/file/XML/forward upload and sending are rejected.', 'Requires zero-status native ACK AND correlated sent record; timeout means delivery unknown, never blindly retry.', 'Uses native decimal-string uint64 IDs rather than v11 numeric int32 message IDs; lossless extension but strict clients may be incompatible.', 'Private peers/user mentions require verified UID facts; reply requires same-peer native reference or exact supplied native metadata.']
actions=[]
for entry in cat['actions']:
    path=BASE/(entry['module']+'.py')
    tree=ast.parse(text(path))
    klass=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name==entry['class'])
    bases=[ast.unparse(x) for x in klass.bases]
    ops=[]; evidence=[]; used_helpers=[]
    if 'PayloadAction' in bases:
        for n in klass.body:
            if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='op' for t in n.targets):
                ops.append(ast.literal_eval(n.value)); evidence.append({'line':n.lineno,'expression':ast.unparse(n)})
        if not ops: ops=[entry['name']]
    for n in ast.walk(tree):
        if not isinstance(n,ast.Call): continue
        func=ast.unparse(n.func)
        if func=='call' and len(n.args)>1 and isinstance(n.args[1],ast.Constant):
            ops.append(n.args[1].value); evidence.append({'line':n.lineno,'expression':ast.unparse(n)})
        elif func in ('send_message','media_info','ctx.rkey.refresh'):
            op={'send_message':'message.send','media_info':'media.url','ctx.rkey.refresh':'rkey'}[func]
            ops.append(op); used_helpers.append(func); evidence.append({'line':n.lineno,'expression':ast.unparse(n)})
    ops=list(dict.fromkeys(ops)); canonical=list(dict.fromkeys(aliases.get(x,x) for x in ops))
    name=entry['name']; caveats=list(notes.get(name,[])); cls='adapter_only'
    if not ops: cls='core_local'
    elif all(x in requirements for x in canonical): cls='headless_verified_contract'
    elif any(x in unresolved for x in canonical) or name=='set_group_special_title': cls='unsupported'
    if name in ('get_msg','get_login_info','get_rkey','send_msg','send_group_msg','send_private_msg','set_group_leave'): cls='conditional'
    if name=='get_version_info':
        imports=[n for n in ast.walk(tree) if isinstance(n,ast.ImportFrom) and any(a.name=='__version__' for a in n.names)]
        if any(n.module is None and n.level!=4 for n in imports):
            cls='unsupported'; caveats.append('Broken relative __version__ import resolves a package that does not export __version__; standard action returns internal error until corrected.')
    for op in canonical:
        if op in unresolved: caveats.append(unresolved[op])
    if cls=='adapter_only': caveats.append('Registered Python adapter only: no matching verified native headless handler; headless fails UNSUPPORTED. Catalog implemented does not establish QQ capability.')
    if 'PayloadAction' in bases and not entry.get('params'): caveats.append('Generic passthrough with empty catalog parameter schema; no action-specific validation in Python.')
    mock=[]
    for op in ops:
        handler=mock_ops.get(op)
        kind='generic_success_echo' if handler is None else 'no_op_success' if handler=='_op_noop' else 'hardcoded_empty_success' if handler in ('_op_empty_list','_op_group_honor','_op_emoji_likes','_op_flash_list','_op_group_files') else 'synthetic_fixture_handler'
        mock.append({'op':op,'handler':handler,'kind':kind})
    native_reqs=[]
    for op in canonical:
        if op in requirements: native_reqs.append({'op':op,'requires':requirements[op]})
    actions.append({'name':name,'standard_v11':name in STANDARD,'category':entry['category'],'module':path.relative_to(ROOT).as_posix(),'python_class':entry['class'],'python_bases':bases,'transport_ops':ops,'headless_canonical_ops':canonical,'implementation_class':cls,'native_requirements':native_reqs,'caveats':caveats,'catalog_status':entry.get('status'),'catalog_wire':entry.get('wire'),'catalog_op':entry.get('op'),'static_call_evidence':evidence,'helpers_followed':used_helpers,'mock_behavior':mock,'pipe_behavior':'shipped native Dispatch rejects every action op; no QQ-side implementation','source_sha256':sources[path.relative_to(ROOT).as_posix()]})
# Count action names and native operation names separately.
counts=dict(collections.Counter(a['implementation_class'] for a in actions))
std_counts=dict(collections.Counter(a['implementation_class'] for a in actions if a['standard_v11']))
available_classes={'core_local','headless_verified_contract','conditional'}
available_std=[a['name'] for a in actions if a['standard_v11'] and a['implementation_class'] in available_classes and a['name']!='get_rkey']
available_ext=[a['name'] for a in actions if not a['standard_v11'] and a['implementation_class'] in available_classes and a['name']!='get_rkey']
report={
 'schema_version':1,'audit_kind':'read-only static source and AST audit','date':'2026-11-11','scope':'Every actions.json entry, Python handlers/helpers, native dispatcher/sender/codec/host, generic PayloadAction, mock transport, shipped C++ pipe scaffold, runtime core.',
 'verification_limits':['No live runtime/API/native/network tests or project imports performed. Official standards documentation fetched separately via read-only web tools.','headless_verified_contract means static signature/result/event/readback contract is implemented; NOT live QQ account or build verification.','Source is concurrently edited by parent; SHA-256 manifest identifies exact inspected snapshot.'],
 'classification_definitions':{'core_local':'Meaningful local query with no transport RPC; does not imply perfect v11 semantic compliance.','headless_verified_contract':'Python route reaches a native handler with explicit static result/event/readback checks; runtime preconditions still apply.','adapter_only':'Python adapter exists but no verified shipped headless implementation.','unsupported':'Explicit native refusal, known unmapped unsupported operation, or broken local implementation.','conditional':'Only a restricted branch/subset works, or local fallback/false-success masking needs qualification.'},
 'standard_source':{'url':'https://11.onebot.dev/api/public.md','retrieval':'HTTP 200 via web_fetch; full 38 public API headings and contracts inspected','github_requested_url':'https://github.com/botuniverse/onebot-11/blob/main/specs/api/public.md','github_retrieval':'GitHub/raw fetch failed; official published documentation accessible','public_api_names':sorted(STANDARD),'count':len(STANDARD)},
 'totals':{'catalog_entries':len(actions),'registry_aliases':len(cat.get('aliases',{})),'exposed_names_including_aliases':len(actions)+len(cat.get('aliases',{})),'standard_registered':sum(a['standard_v11'] for a in actions),'extension_entries':sum(not a['standard_v11'] for a in actions),'implementation_classes':counts,'standard_implementation_classes':std_counts,'native_canonical_contract_operations':len(requirements),'catalog_api_names_reaching_native_contracts':sum(bool(a['headless_canonical_ops']) and all(op in requirements for op in a['headless_canonical_ops']) for a in actions),'generic_payload_action_modules':sum('PayloadAction' in a['python_bases'] for a in actions)},
 'standard_missing_registration':sorted(STANDARD-{a['name'] for a in actions}),
 'standard_unavailable_as_implemented':[a['name'] for a in actions if a['standard_v11'] and a['implementation_class'] in ('adapter_only','unsupported')],
 'available_standard_names_with_caveats':available_std,
 'available_extension_names_with_caveats':available_ext,
 'not_counted_available_despite_success_shape':['get_rkey'],
 'all_extension_names':[a['name'] for a in actions if not a['standard_v11']],
 'registry_aliases':cat.get('aliases',{}),
 'registry_alias_caveats':{'get_group_member_list_no_cache':'Alias does not set no_cache, and target handler discards that argument; not a forced-refresh implementation.','send_group_poke':'Target group_poke routes to unsupported poke.send.','send_friend_poke':'Target friend_poke routes to unsupported poke.send.'},
 'native_canonical_contracts':requirements,
 'native_aliases':aliases,
 'native_explicit_unsupported':unresolved,
 'global_findings':[
  '241 catalog entries are Python action registrations, not 241 QQ abilities; 26 canonical native operations are not 26 OneBot APIs (3 send APIs share message.send).',
  'MockTransport returns success for all unknown operations and has explicit no-op/empty fixtures; mock passing is not native evidence.',
  'Shipped C++ Dispatch rejects all action operations; pipe mode does not inherit headless implementation.',
  'base.call discards typed transport status and raises generic UPSTREAM; native UNSUPPORTED/NOT_FOUND/INVALID_PARAMS can all become same OneBot retcode.',
  'base.call returns result.data or {}, masking empty list as empty object; adapters sometimes restore expected list shape.',
  'Runtime does not implement restart/cache.clean RPC locally; source metadata wire=core is not evidence that no payload work is required.',
  'tools/gen_payload_ops.py and docs/payload-ops.md infer behavior from stale wire metadata; docs claim action names always equal op names and 68 core-backed APIs need no payload work, which is false.',
  'Native capabilities only check service presence/signatures; cache readiness, rights, UID maps, receipt data, and API result schemas remain required at execution.',
  'No source action inspected is an unconditional local mutation no-op in this snapshot; fake mutation success is in mock generic/no-op paths. Native desired-state already matching is verified idempotent success, not fake success.'
 ],
 'suggested_corrections':[
  'Keep corrected get_version_info root import and fail-closed get_login_info; add regression coverage that cannot be satisfied by mock generic success.',
  'Expose support by transport and static/runtime capability class, not implemented/wire flags; retain exact op mapping.',
  'Preserve typed native error retcodes in base.call instead of collapsing all to UPSTREAM.',
  'Make mock unsupported operations fail by default or conspicuously label simulation; never advertise its no-op/empty responses as native support.',
  'Report rkey fetch failure rather than successful [] unless a deliberately documented cached-only mode is requested.',
  'Complete v11 member fields without inventing unknown data; retain corrected forward result shape, implement native forward retrieval, domain forwarding, out_format conversion, local media file semantics, request type alias.',
  'Implement a real asynchronous restart with delay or explicitly reject it; implement real cache cleanup or explicitly reject it.',
  'Derive can_send_image/record from actual send capabilities; pipe scaffold must not report true.',
  'Forward no_cache consistently, remove misleading no-cache alias behavior, and clear/account-scope caches on identity changes.',
  'Document uint64 string message-ID extension and supported send segments rather than claim full v11 wire compatibility.'
 ],
 'parent_corrections_reflected':['get_version_info root import corrected; core_local now usable.','get_login_info now checks login readiness and propagates failure; conditional only because it needs valid identity.','set_group_special_title now routes group.special_title; still unsupported natively.','get_forward_msg now exposes standard message nodes plus legacy messages; still adapter_only without native retrieval.','CQ parsing expanded; native sending remains restricted to supported codec element types.'],
 'actions':actions,'source_sha256_manifest':sources,
}
out=ROOT/'temp/onebot-action-audit.json'
if out.exists():
    # Existing audit was inspected before authorized regeneration; never silently overwrite.
    import sys
    if '--regenerate-inspected-audit' not in sys.argv:
        raise SystemExit('Refusing to overwrite existing audit; inspect it then pass --regenerate-inspected-audit.')
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'totals':report['totals'],'standard_unavailable':report['standard_unavailable_as_implemented'],'available_standard':available_std,'available_extensions':available_ext},ensure_ascii=False,indent=2))
