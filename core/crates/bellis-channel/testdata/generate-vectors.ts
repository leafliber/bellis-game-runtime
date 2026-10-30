/**
 * 从 Bellis 的 TypeScript 实现导出 `bellis-channel/1` 的已知答案测试向量（vectors.json）。
 *
 * 向量全部由 Bellis 的代码产生：握手与 JSON-RPC 的字节取自真实的 Connection 与 RpcPeer，
 * 随机数用固定值替换 `node:crypto` 的 randomBytes；消息校验的结论取自 Bellis 用的同一份 TypeBox 校验；
 * 长度上限与超时的取值从 Bellis 源码读出。本仓库的 Python 与 Rust 实现都以这些向量做测试。
 *
 * 用法见 docs/modules/core-protocol.md 的“本机通道”：
 *   node generate-vectors.ts <Bellis 导出目录> <Bellis 提交> > vectors.json
 * 导出目录里须有 `git archive` 导出的 packages/channel 与 packages/protocol，
 * 以及 node_modules/typebox（Bellis 锁定的版本）和指向 packages/protocol 的 node_modules/@bellis/protocol。
 */
import crypto from 'node:crypto';
import { readFileSync } from 'node:fs';
import { createRequire, syncBuiltinESMExports } from 'node:module';
import { join } from 'node:path';
import { Duplex } from 'node:stream';
import { pathToFileURL } from 'node:url';

const [root, commit] = process.argv.slice(2);
if (root === undefined || commit === undefined || !/^[0-9a-f]{40}$/.test(commit)) {
  console.error('用法: node generate-vectors.ts <Bellis 导出目录> <Bellis 提交的完整哈希>');
  process.exit(2);
}

// ---------------------------------------------------------------------------
// 固定随机数：Bellis 的 Connection 与 address 都经 node:crypto 的 randomBytes 取随机数。
// ---------------------------------------------------------------------------
const createHmac = crypto.createHmac;
const queue: Buffer[] = [];
crypto.randomBytes = ((size: number) => {
  const next = queue.shift();
  if (next === undefined || next.length !== size) {
    throw new Error(`randomBytes(${size}) 没有预置的值`);
  }
  return Buffer.from(next);
}) as typeof crypto.randomBytes;
syncBuiltinESMExports();

const load = (relative: string) => import(pathToFileURL(join(root, relative)).href);
const channel = await load('packages/channel/src/index.ts');
const protocol = await load('packages/protocol/src/index.ts');
const requireFromProtocol = createRequire(join(root, 'packages/protocol/package.json'));
const { Compile } = await import(
  pathToFileURL(requireFromProtocol.resolve('typebox/compile')).href
);

// ---------------------------------------------------------------------------
// 源码里的常量（未导出的也从源码读，读不到即失败）。
// ---------------------------------------------------------------------------
function constant(file: string, name: string): number | string {
  const text = readFileSync(join(root, 'packages/channel/src', file), 'utf8');
  const match = new RegExp(`const ${name} = ([^;]+);`).exec(text);
  if (match === null) throw new Error(`${file} 里找不到常量 ${name}`);
  const expression = (match[1] as string).trim();
  const quoted = /^'([^']*)'$/.exec(expression);
  if (quoted !== null) return quoted[1] as string;
  if (!/^[0-9_ *]+$/.test(expression)) throw new Error(`无法求值 ${name} = ${expression}`);
  return expression
    .split('*')
    .map((part) => Number(part.trim().replaceAll('_', '')))
    .reduce((a, b) => a * b, 1);
}

const constants = {
  proof_domain: constant('connection.ts', 'PROOF_DOMAIN'),
  frame_header_bytes: channel.FRAME_HEADER_BYTES,
  handshake_max_frame_bytes: constant('connection.ts', 'HANDSHAKE_MAX_FRAME_BYTES'),
  default_max_frame_bytes: constant('connection.ts', 'DEFAULT_MAX_FRAME_BYTES'),
  default_handshake_timeout_ms: constant('connection.ts', 'DEFAULT_HANDSHAKE_TIMEOUT_MS'),
  close_grace_ms: constant('connection.ts', 'CLOSE_GRACE_MS'),
  default_max_pending_handshakes: constant('endpoint.ts', 'DEFAULT_MAX_PENDING_HANDSHAKES'),
  default_rpc_max_inflight: constant('rpc.ts', 'DEFAULT_MAX_INFLIGHT'),
  session_key_bytes: channel.SESSION_KEY_BYTES,
  max_uds_path_bytes: constant('address.ts', 'MAX_UDS_PATH_BYTES'),
};

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------
const hex = (bytes: Uint8Array) => Buffer.from(bytes).toString('hex');
const bytesFrom = (start: number) => Buffer.from(Array.from({ length: 32 }, (_, i) => start + i));
const b64 = (bytes: Uint8Array) => Buffer.from(bytes).toString('base64url');
const settle = () => new Promise((resolve) => setTimeout(resolve, 20));

const KEY = bytesFrom(0x00);
const OTHER_KEY = bytesFrom(0x40);
const ROLE = 'game-runtime.core';
const CLIENT_NONCE = bytesFrom(0x80);
const SERVER_NONCE = bytesFrom(0xa0);
const CLIENT_NONCE_2 = bytesFrom(0xc0);
const SERVER_NONCE_2 = bytesFrom(0xe0);

function mac(
  key: Uint8Array,
  direction: string,
  role: string,
  clientNonce: string,
  serverNonce: string,
  domain = constants.proof_domain,
): string {
  const record = JSON.stringify([domain, direction, role, clientNonce, serverNonce]);
  return createHmac('sha256', key).update(record).digest('base64url');
}

const frame = (kind: number, body: object | string | Buffer) =>
  channel.encodeFrame(
    kind,
    Buffer.isBuffer(body)
      ? body
      : Buffer.from(typeof body === 'string' ? body : JSON.stringify(body), 'utf8'),
  ) as Buffer;
const handshake = (body: object | string | Buffer) => frame(channel.FrameKind.Handshake, body);
const json = (body: object | string | Buffer) => frame(channel.FrameKind.Json, body);

/** 把一段字节按帧拆开，便于审阅；JSON 与握手帧附带负载文本。 */
function describe(bytes: Buffer): Array<Record<string, unknown>> {
  const decoder = new channel.FrameDecoder(0xffffffff);
  decoder.push(bytes);
  const frames: Array<Record<string, unknown>> = [];
  for (let next = decoder.read(); next; next = decoder.read()) {
    const entry: Record<string, unknown> = {
      kind: next.kind,
      frame_hex: hex(channel.encodeFrame(next.kind, next.payload)),
    };
    if (next.kind !== channel.FrameKind.Binary) entry.text = next.payload.toString('utf8');
    frames.push(entry);
  }
  if (decoder.bufferedBytes !== 0) throw new Error('输出里有不完整的帧');
  return frames;
}

type Side = 'client' | 'server';
type Wire = Array<{ from: Side; bytes: Buffer }>;

/**
 * 一对内存中的字节流，并记录两端写出的全部字节（Bellis 的每次 write 正好是一帧）。
 * 与真实的 socket 一样，写入在下一轮事件循环才送达对端：Bellis 的 Connection 先写出证明、
 * 后切换握手状态，同步送达会让对端的 accept 在状态切换前重入。
 */
function socketPair(sides: [Side, Side], wire: Wire): [Duplex, Duplex] {
  const streams: Duplex[] = [];
  for (const [index, side] of sides.entries()) {
    const other = () => streams[1 - index] as Duplex;
    streams.push(
      new Duplex({
        allowHalfOpen: false,
        read() {},
        write(chunk: Buffer, _encoding, callback) {
          wire.push({ from: side, bytes: Buffer.from(chunk) });
          setImmediate(() => {
            other().push(chunk);
            callback();
          });
        },
        final(callback) {
          setImmediate(() => {
            other().push(null);
            callback();
          });
        },
        destroy(error, callback) {
          setImmediate(() => other().destroy());
          callback(error);
        },
      }),
    );
  }
  return [streams[0] as Duplex, streams[1] as Duplex];
}

// ---------------------------------------------------------------------------
// 帧编解码
// ---------------------------------------------------------------------------
const frameVectors = [
  { kind: 0, payload: Buffer.alloc(0) },
  { kind: 1, payload: Buffer.from('{"a":1}') },
  { kind: 2, payload: Buffer.from([0, 1, 2, 0xff]) },
  { kind: 1, payload: Buffer.from('{"text":"你好"}') },
  { kind: 2, payload: Buffer.alloc(300, 7) },
].map(({ kind, payload }) => ({
  kind,
  payload_hex: hex(payload),
  frame_hex: hex(channel.encodeFrame(kind, payload)),
}));

function decoderCase(name: string, maxPayloadBytes: number, input: Buffer) {
  const decoder = new channel.FrameDecoder(maxPayloadBytes);
  decoder.push(input);
  const frames: Array<{ kind: number; payload_hex: string }> = [];
  let error: string | null = null;
  try {
    for (let next = decoder.read(); next; next = decoder.read()) {
      frames.push({ kind: next.kind, payload_hex: hex(next.payload) });
    }
  } catch (caught) {
    if (!(caught instanceof channel.ChannelError)) throw caught;
    error = caught.code;
  }
  return {
    name,
    max_payload_bytes: maxPayloadBytes,
    input_hex: hex(input),
    frames,
    error,
    buffered_bytes: error === null ? decoder.bufferedBytes : null,
  };
}

const header = (length: number, kind: number) => {
  const bytes = Buffer.alloc(5);
  bytes.writeUInt32BE(length, 0);
  bytes.writeUInt8(kind, 4);
  return bytes;
};

const decoderVectors = [
  decoderCase(
    'two-frames',
    64,
    Buffer.concat([frame(0, Buffer.from('ab')), frame(2, Buffer.from([9]))]),
  ),
  decoderCase('exactly-max', 4, frame(1, Buffer.from('abcd'))),
  decoderCase('over-max-header-only', 4, header(5, 1)),
  decoderCase('unknown-kind', 64, header(0, 9)),
  decoderCase('unknown-kind-checked-before-length', 4, header(0xffffffff, 3)),
  decoderCase('max-length-field', 1024, header(0xffffffff, 1)),
  decoderCase('partial-header', 64, Buffer.from([0, 0, 0])),
  decoderCase('partial-payload', 64, Buffer.concat([header(4, 1), Buffer.from('ab')])),
];

// ---------------------------------------------------------------------------
// 地址与会话密钥
// ---------------------------------------------------------------------------
const PATH_RANDOM = Buffer.from(Array.from({ length: 16 }, (_, i) => i * 17));
queue.push(PATH_RANDOM);
const windowsPath = channel.createChannelPath('s1', 'win32');
queue.push(PATH_RANDOM);
const posixPath = channel.createChannelPath('s1', 'linux') as string;
// 在 Windows 上生成时，`join(tmpdir(), …)` 用反斜杠分隔。
const posixName = posixPath.split(/[\\/]/).at(-1) as string;

function sessionIdValid(id: string): boolean {
  try {
    queue.push(PATH_RANDOM);
    channel.createChannelPath(id, 'win32');
    return true;
  } catch (caught) {
    queue.length = 0;
    if (caught instanceof channel.ChannelError && caught.code === 'address-invalid') return false;
    throw caught;
  }
}

function decodeKey(text: string): string | null {
  try {
    return hex(channel.decodeSessionKey(text));
  } catch (caught) {
    if (caught instanceof channel.ChannelError) return null;
    throw caught;
  }
}

const keyText = channel.encodeSessionKey(KEY);
const nonCanonicalKey = `${keyText.slice(0, -1)}${String.fromCharCode(keyText.charCodeAt(keyText.length - 1) + 1)}`;
const addressVectors = {
  random_hex: hex(PATH_RANDOM),
  windows: { session_id: 's1', path: windowsPath },
  posix: { session_id: 's1', file_name: posixName },
  session_ids: ['s1', 'a'.repeat(16), 'a'.repeat(17), '', '../x', 'A1', 'ab-c', '0'].map((id) => ({
    session_id: id,
    valid: sessionIdValid(id),
  })),
  session_keys: [
    { text: keyText },
    { text: `${keyText}=` },
    { text: 'not a key' },
    { text: channel.encodeSessionKey(KEY.subarray(1)) },
    { text: nonCanonicalKey },
    { text: Buffer.from(KEY).toString('base64') },
  ].map(({ text }) => ({ text, key_hex: decodeKey(text) })),
};

// ---------------------------------------------------------------------------
// 一次完整会话：握手与 JSON-RPC 的往来
// ---------------------------------------------------------------------------
async function session() {
  const wire: Wire = [];
  const [clientSide, serverSide] = socketPair(['client', 'server'], wire);
  queue.push(CLIENT_NONCE, SERVER_NONCE);
  const serverReady = channel.Connection.accept(serverSide, { key: KEY });
  const client = await channel.Connection.open(clientSide, ROLE, { key: KEY });
  const server = await serverReady;
  if (server.role !== ROLE) throw new Error('角色不符');
  await settle();
  const handshakeFrames = wire
    .splice(0)
    .map(({ from, bytes }) => ({ from, ...describe(bytes)[0] }));

  const clientRpc = new channel.RpcPeer(client, { timeoutMs: 1000 });
  const serverRpc = new channel.RpcPeer(server, { timeoutMs: 1000 });
  serverRpc.handle('math.add', (params: { a: number; b: number }) => params.a + params.b);
  clientRpc.handle('host.ping', () => undefined);
  serverRpc.handle('a.fail', () => {
    throw new channel.RpcError(1001, '不允许', { reason: 'x' });
  });
  serverRpc.handle('a.crash', () => {
    throw new Error('内部细节');
  });
  serverRpc.handle('a.hang', () => new Promise(() => {}));
  const notifications: unknown[] = [];
  serverRpc.onNotification('host.heartbeat', (params: unknown) => notifications.push(params));

  const steps: Array<Record<string, unknown>> = [];
  async function step(name: string, outcome: Record<string, unknown>, run: () => Promise<unknown>) {
    let result: unknown;
    let error: unknown = null;
    try {
      result = await run();
    } catch (caught) {
      error =
        caught instanceof channel.RpcError
          ? { rpc: { code: caught.code, message: caught.message, data: caught.data ?? null } }
          : caught instanceof channel.ChannelError
            ? { channel: caught.code }
            : String(caught);
    }
    await settle();
    const frames = wire.splice(0).map(({ from, bytes }) => ({ from, ...describe(bytes)[0] }));
    steps.push({ name, ...outcome, result: result ?? null, error, frames });
  }

  await step(
    'client-request',
    { actor: 'client', call: 'request', method: 'math.add', params: { a: 1, b: 2 } },
    () => clientRpc.request('math.add', { a: 1, b: 2 }),
  );
  await step(
    'server-request',
    { actor: 'server', call: 'request', method: 'host.ping', params: null },
    () => serverRpc.request('host.ping'),
  );
  await step(
    'client-notify',
    { actor: 'client', call: 'notify', method: 'host.heartbeat', params: { seq: 7 } },
    async () => clientRpc.notify('host.heartbeat', { seq: 7 }),
  );
  await step(
    'client-notify-no-params',
    { actor: 'client', call: 'notify', method: 'host.heartbeat', params: null },
    async () => clientRpc.notify('host.heartbeat'),
  );
  await step(
    'method-not-found',
    { actor: 'client', call: 'request', method: 'no.such', params: null },
    () => clientRpc.request('no.such'),
  );
  await step(
    'handler-rpc-error',
    { actor: 'client', call: 'request', method: 'a.fail', params: null },
    () => clientRpc.request('a.fail'),
  );
  await step(
    'handler-internal-error',
    { actor: 'client', call: 'request', method: 'a.crash', params: null },
    () => clientRpc.request('a.crash'),
  );
  const invalid = { jsonrpc: '2.0', method: 'Bad Name', id: 1 };
  await step('invalid-request', { actor: 'client', call: 'raw', raw: invalid }, async () =>
    client.send(invalid),
  );
  const stringId = { jsonrpc: '2.0', id: 'req-1', method: 'math.add', params: { a: 2, b: 3 } };
  await step('string-id', { actor: 'client', call: 'raw', raw: stringId }, async () =>
    client.send(stringId),
  );
  await step(
    'request-timeout',
    { actor: 'client', call: 'request', method: 'a.hang', params: {}, timeout_ms: 20 },
    () => clientRpc.request('a.hang', {}, { timeoutMs: 20 }),
  );
  await step('server-binary', { actor: 'server', call: 'binary', binary_hex: '010203' }, () =>
    server.sendBinary(Buffer.from([1, 2, 3])),
  );
  client.close();
  await settle();
  if (JSON.stringify(notifications) !== JSON.stringify([{ seq: 7 }, {}])) {
    throw new Error(`通知不符：${JSON.stringify(notifications)}`);
  }

  // 自检：文档里写的证明公式与真实握手里的证明一致。
  const clientNonceText = b64(CLIENT_NONCE);
  const serverNonceText = b64(SERVER_NONCE);
  const challenge = JSON.parse(handshakeFrames[1]?.text as string);
  const proof = JSON.parse(handshakeFrames[2]?.text as string);
  if (challenge.proof !== mac(KEY, 'server', ROLE, clientNonceText, serverNonceText)) {
    throw new Error('服务端证明与公式不符');
  }
  if (proof.proof !== mac(KEY, 'client', ROLE, clientNonceText, serverNonceText)) {
    throw new Error('客户端证明与公式不符');
  }

  return {
    key_hex: hex(KEY),
    role: ROLE,
    client_nonce_hex: hex(CLIENT_NONCE),
    server_nonce_hex: hex(SERVER_NONCE),
    mac_record: JSON.stringify([
      constants.proof_domain,
      'server',
      ROLE,
      clientNonceText,
      serverNonceText,
    ]),
    handshake: handshakeFrames,
    rpc: steps,
  };
}

// ---------------------------------------------------------------------------
// 握手的各种结局：一端是 Bellis 的 Connection，另一端按脚本一次写入全部输入。
// ---------------------------------------------------------------------------
interface CaseInput {
  note: string;
  nonce: Buffer;
  input: Buffer[];
  end?: boolean;
  maxFrameBytes?: number;
  role?: string;
}

async function runCase(side: Side, name: string, spec: CaseInput) {
  queue.length = 0;
  queue.push(spec.nonce);
  const wire: Wire = [];
  const [local, peer] = socketPair([side, side === 'server' ? 'client' : 'server'], wire);
  const options = {
    key: KEY,
    ...(spec.maxFrameBytes === undefined ? {} : { maxFrameBytes: spec.maxFrameBytes }),
  };
  const role = spec.role ?? ROLE;
  const ready =
    side === 'server'
      ? channel.Connection.accept(local, options)
      : channel.Connection.open(local, role, options);
  peer.on('data', () => {});
  peer.write(Buffer.concat(spec.input));
  if (spec.end) peer.end();
  const delivered: Array<Record<string, unknown>> = [];
  let result: string;
  let closeError: string | null = null;
  try {
    const connection = await ready;
    result = 'open';
    connection.on('message', (message: unknown) =>
      delivered.push({ json: JSON.stringify(message) }),
    );
    connection.on('binary', (data: Buffer) => delivered.push({ binary_hex: hex(data) }));
    connection.on('close', (error?: { code: string }) => {
      closeError = error?.code ?? 'closed';
    });
  } catch (caught) {
    if (!(caught instanceof channel.ChannelError)) throw caught;
    result = caught.code;
  }
  await settle();
  queue.length = 0;
  return {
    name,
    side,
    note: spec.note,
    role,
    nonce_hex: hex(spec.nonce),
    max_frame_bytes: spec.maxFrameBytes ?? constants.default_max_frame_bytes,
    input_hex: spec.input.map(hex),
    end: spec.end ?? false,
    output: wire.filter(({ from }) => from === side).flatMap(({ bytes }) => describe(bytes)),
    result,
    delivered,
    close_error: result === 'open' ? closeError : null,
  };
}

const cn = b64(CLIENT_NONCE);
const sn = b64(SERVER_NONCE);
const hello = (nonce = cn, role = ROLE) => ({ v: 1, type: 'hello', role, nonce });
const proofFor = (value: string) => ({ v: 1, type: 'proof', proof: value });
const challengeFor = (nonce: string, value: string) => ({
  v: 1,
  type: 'challenge',
  nonce,
  proof: value,
});
const goodClientProof = mac(KEY, 'client', ROLE, cn, sn);
const goodServerProof = mac(KEY, 'server', ROLE, cn, sn);

/** base64url 的第 43 个字符只用到高 4 位；改动低 2 位后 Node 解出的字节不变。 */
function nonCanonical(text: string): string {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
  const last = alphabet.indexOf(text.at(-1) as string);
  return text.slice(0, -1) + alphabet[last ^ 1];
}

function flipFirstBit(text: string): string {
  const bytes = Buffer.from(text, 'base64url');
  bytes[0] = (bytes[0] as number) ^ 1;
  return b64(bytes);
}

function padHello(totalBytes: number): Buffer {
  const base = JSON.stringify(hello());
  return Buffer.from(base + ' '.repeat(totalBytes - Buffer.byteLength(base)));
}

function jsonPayload(totalBytes: number): Buffer {
  const base = '{"pad":""}';
  return Buffer.from(`{"pad":"${'x'.repeat(totalBytes - base.length)}"}`);
}

const serverCases: Array<[string, CaseInput]> = [
  [
    'valid',
    {
      note: '正常握手，accept 后紧跟的业务帧不丢',
      nonce: SERVER_NONCE,
      input: [handshake(hello()), handshake(proofFor(goodClientProof)), json({ n: 1 })],
    },
  ],
  [
    'data-before-auth',
    {
      note: '认证完成前发送业务数据',
      nonce: SERVER_NONCE,
      input: [handshake(hello()), json({ jsonrpc: '2.0', method: 'x' })],
    },
  ],
  [
    'data-first',
    {
      note: '第一帧就是业务数据',
      nonce: SERVER_NONCE,
      input: [json({ jsonrpc: '2.0', method: 'a.b' })],
    },
  ],
  [
    'binary-first',
    { note: '第一帧是二进制帧', nonce: SERVER_NONCE, input: [frame(2, Buffer.from([1]))] },
  ],
  [
    'tampered-proof',
    {
      note: '客户端证明被篡改一位',
      nonce: SERVER_NONCE,
      input: [handshake(hello()), handshake(proofFor(flipFirstBit(goodClientProof)))],
    },
  ],
  [
    'reflected-proof',
    {
      note: '把服务端的证明原样回送',
      nonce: SERVER_NONCE,
      input: [handshake(hello()), handshake(proofFor(goodServerProof))],
    },
  ],
  [
    'replayed-handshake',
    {
      note: '重放上一次连接的 hello 与 proof，服务端随机数已变',
      nonce: SERVER_NONCE_2,
      input: [handshake(hello()), handshake(proofFor(goodClientProof))],
    },
  ],
  [
    'wrong-key',
    {
      note: '客户端用了别的密钥',
      nonce: SERVER_NONCE,
      input: [handshake(hello()), handshake(proofFor(mac(OTHER_KEY, 'client', ROLE, cn, sn)))],
    },
  ],
  [
    'wrong-domain',
    {
      note: '客户端的证明用了别的协议标识',
      nonce: SERVER_NONCE,
      input: [
        handshake(hello()),
        handshake(proofFor(mac(KEY, 'client', ROLE, cn, sn, 'bellis-channel/2'))),
      ],
    },
  ],
  [
    'wrong-role-in-proof',
    {
      note: '证明里的角色与 hello 不同',
      nonce: SERVER_NONCE,
      input: [handshake(hello()), handshake(proofFor(mac(KEY, 'client', 'other.role', cn, sn)))],
    },
  ],
  [
    'non-canonical-proof',
    {
      note: '证明的最后一个字符低位不同，解出的字节相同',
      nonce: SERVER_NONCE,
      input: [handshake(hello()), handshake(proofFor(nonCanonical(goodClientProof)))],
    },
  ],
  [
    'hello-version-2',
    { note: 'hello 的版本不是 1', nonce: SERVER_NONCE, input: [handshake({ ...hello(), v: 2 })] },
  ],
  [
    'hello-version-float',
    {
      note: 'hello 的版本写成 1.0',
      nonce: SERVER_NONCE,
      input: [
        handshake(JSON.stringify(hello()).replace('"v":1', '"v":1.0')),
        handshake(proofFor(goodClientProof)),
      ],
    },
  ],
  ['hello-not-json', { note: 'hello 不是 JSON', nonce: SERVER_NONCE, input: [handshake('hello')] }],
  [
    'hello-bad-role',
    {
      note: '角色名不合法',
      nonce: SERVER_NONCE,
      input: [handshake({ ...hello(), role: 'Bad Role' })],
    },
  ],
  [
    'proof-bad-format',
    {
      note: 'proof 的长度不对',
      nonce: SERVER_NONCE,
      input: [handshake(hello()), handshake(proofFor(goodClientProof.slice(1)))],
    },
  ],
  [
    'unknown-kind',
    { note: '未知的帧类型', nonce: SERVER_NONCE, input: [frame(3, Buffer.alloc(0))] },
  ],
  [
    'hello-at-limit',
    {
      note: '认证前帧长正好等于上限',
      nonce: SERVER_NONCE,
      input: [
        handshake(padHello(Number(constants.handshake_max_frame_bytes))),
        handshake(proofFor(goodClientProof)),
      ],
    },
  ],
  [
    'oversize-before-auth',
    {
      note: '认证前帧长超过上限',
      nonce: SERVER_NONCE,
      input: [handshake(padHello(Number(constants.handshake_max_frame_bytes) + 1))],
    },
  ],
  [
    'eof-after-hello',
    { note: '握手完成前对端关闭', nonce: SERVER_NONCE, input: [handshake(hello())], end: true },
  ],
  [
    'handshake-after-open',
    {
      note: '认证后再收到握手帧',
      nonce: SERVER_NONCE,
      input: [handshake(hello()), handshake(proofFor(goodClientProof)), handshake(hello())],
    },
  ],
  [
    'oversize-after-auth',
    {
      note: '认证后帧长等于上限的送达，超过上限的断开',
      nonce: SERVER_NONCE,
      maxFrameBytes: 64,
      input: [
        handshake(hello()),
        handshake(proofFor(goodClientProof)),
        json(jsonPayload(64)),
        json(jsonPayload(65)),
      ],
    },
  ],
  [
    'invalid-json-after-open',
    {
      note: '认证后收到不是 JSON 的 JSON 帧',
      nonce: SERVER_NONCE,
      input: [handshake(hello()), handshake(proofFor(goodClientProof)), json('{')],
    },
  ],
];

const clientCases: Array<[string, CaseInput]> = [
  [
    'valid',
    {
      note: '正常握手，accept 后紧跟的业务帧不丢',
      nonce: CLIENT_NONCE,
      input: [
        handshake(challengeFor(sn, goodServerProof)),
        handshake({ v: 1, type: 'accept' }),
        json({ n: 1 }),
      ],
    },
  ],
  [
    'tampered-proof',
    {
      note: '服务端证明被篡改一位',
      nonce: CLIENT_NONCE,
      input: [handshake(challengeFor(sn, flipFirstBit(goodServerProof)))],
    },
  ],
  [
    'replayed-challenge',
    {
      note: '重放上一次连接的 challenge，客户端随机数已变',
      nonce: CLIENT_NONCE_2,
      input: [handshake(challengeFor(sn, goodServerProof))],
    },
  ],
  [
    'wrong-key',
    {
      note: '服务端用了别的密钥（地址被抢注）',
      nonce: CLIENT_NONCE,
      input: [handshake(challengeFor(sn, mac(OTHER_KEY, 'server', ROLE, cn, sn)))],
    },
  ],
  [
    'wrong-domain',
    {
      note: '服务端的证明用了别的协议标识',
      nonce: CLIENT_NONCE,
      input: [handshake(challengeFor(sn, mac(KEY, 'server', ROLE, cn, sn, 'bellis-channel/2')))],
    },
  ],
  [
    'reflected-hello',
    {
      note: '服务端以客户端方向的证明回应',
      nonce: CLIENT_NONCE,
      input: [handshake(challengeFor(sn, mac(KEY, 'client', ROLE, cn, sn)))],
    },
  ],
  [
    'non-canonical-proof',
    {
      note: '证明的最后一个字符低位不同，解出的字节相同',
      nonce: CLIENT_NONCE,
      input: [
        handshake(challengeFor(sn, nonCanonical(goodServerProof))),
        handshake({ v: 1, type: 'accept' }),
      ],
    },
  ],
  [
    'challenge-version-2',
    {
      note: 'challenge 的版本不是 1',
      nonce: CLIENT_NONCE,
      input: [handshake({ ...challengeFor(sn, goodServerProof), v: 2 })],
    },
  ],
  [
    'data-before-auth',
    {
      note: '认证完成前收到业务数据',
      nonce: CLIENT_NONCE,
      input: [json({ jsonrpc: '2.0', method: 'a.b' })],
    },
  ],
  [
    'oversize-before-auth',
    {
      note: '认证前帧长超过上限',
      nonce: CLIENT_NONCE,
      input: [header(Number(constants.handshake_max_frame_bytes) + 1, 0)],
    },
  ],
  [
    'bad-accept',
    {
      note: 'accept 不合法',
      nonce: CLIENT_NONCE,
      input: [handshake(challengeFor(sn, goodServerProof)), handshake({ v: 1, type: 'acept' })],
    },
  ],
  [
    'data-instead-of-accept',
    {
      note: '收到证明后服务端没有回 accept 而是业务数据',
      nonce: CLIENT_NONCE,
      input: [handshake(challengeFor(sn, goodServerProof)), json({ n: 1 })],
    },
  ],
  ['eof-after-hello', { note: '握手完成前对端关闭', nonce: CLIENT_NONCE, input: [], end: true }],
  [
    'oversize-after-auth',
    {
      note: '认证后帧长等于上限的送达，超过上限的断开',
      nonce: CLIENT_NONCE,
      maxFrameBytes: 64,
      input: [
        handshake(challengeFor(sn, goodServerProof)),
        handshake({ v: 1, type: 'accept' }),
        json(jsonPayload(64)),
        json(jsonPayload(65)),
      ],
    },
  ],
  [
    'handshake-after-open',
    {
      note: '认证后再收到握手帧',
      nonce: CLIENT_NONCE,
      input: [
        handshake(challengeFor(sn, goodServerProof)),
        handshake({ v: 1, type: 'accept' }),
        handshake({ v: 1, type: 'accept' }),
      ],
    },
  ],
];

// ---------------------------------------------------------------------------
// 认证后 JSON 帧负载的解析：负载字节 → 送达的值或 protocol-error。
// ---------------------------------------------------------------------------
const payloadCases: Array<[string, Buffer]> = [
  ['object', Buffer.from('{"a":1}')],
  ['whitespace', Buffer.from(' \t\r\n{"a":1} \n')],
  ['top-level-string', Buffer.from('"text"')],
  ['non-ascii', Buffer.from('{"text":"你好"}')],
  ['escaped-non-ascii', Buffer.from('{"text":"\\u4f60\\u597d"}')],
  ['duplicate-key-last-wins', Buffer.from('{"a":1,"a":2}')],
  [
    'invalid-utf8-replaced',
    Buffer.concat([Buffer.from('{"a":"'), Buffer.from([0xff, 0xe2, 0x82]), Buffer.from('"}')]),
  ],
  ['lone-surrogate-escape', Buffer.from('{"a":"\\ud800"}')],
  ['nesting-200', Buffer.from(`${'['.repeat(200)}${']'.repeat(200)}`)],
  ['bom', Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), Buffer.from('{}')])],
  ['nan', Buffer.from('NaN')],
  ['infinity', Buffer.from('{"a":Infinity}')],
  ['trailing-data', Buffer.from('{"a":1}{"b":2}')],
  ['trailing-comma', Buffer.from('{"a":1,}')],
  ['single-quotes', Buffer.from("{'a':1}")],
  ['empty', Buffer.alloc(0)],
  ['utf16', Buffer.from('{}', 'utf16le')],
  ['raw-control-char', Buffer.from('{"a":"\u0001"}')],
];

async function payloadCase(name: string, payload: Buffer) {
  const outcome = await runCase('server', `payload-${name}`, {
    note: '',
    nonce: SERVER_NONCE,
    input: [handshake(hello()), handshake(proofFor(goodClientProof)), json(payload)],
  });
  if (outcome.result !== 'open') throw new Error(`负载用例 ${name} 握手失败`);
  const delivered = outcome.delivered[0] as { json?: string } | undefined;
  return {
    name,
    payload_hex: hex(payload),
    valid: delivered !== undefined,
    value_json: delivered?.json ?? null,
    error: delivered === undefined ? outcome.close_error : null,
  };
}

// ---------------------------------------------------------------------------
// 消息校验：Bellis 用的同一份 TypeBox 校验的结论。
// ---------------------------------------------------------------------------
const check = (schema: unknown) => {
  const compiled = Compile(schema);
  return (text: string) => compiled.Check(JSON.parse(text)) as boolean;
};
const isHello = check(protocol.ChannelHello);
const isChallenge = check(protocol.ChannelChallenge);
const isProof = check(protocol.ChannelProof);
const isAccept = check(protocol.ChannelAccept);
const isRequest = Compile(protocol.RpcRequest);
const isNotification = Compile(protocol.RpcNotification);
const isSuccess = Compile(protocol.RpcSuccess);
const isFailure = Compile(protocol.RpcFailure);
const isCancel = check(protocol.RpcCancelParams);

/** 与 rpc.ts 的 #receive 同样的分类顺序。 */
function classify(text: string): string {
  const message = JSON.parse(text);
  if (isRequest.Check(message)) return 'request';
  if (isNotification.Check(message)) return 'notification';
  if (isSuccess.Check(message)) return 'success';
  if (isFailure.Check(message)) return 'failure';
  return 'invalid';
}

const nonceText = cn;
const helloTexts = [
  JSON.stringify(hello()),
  JSON.stringify(hello()).replace('"v":1', '"v":1.0'),
  JSON.stringify(hello()).replace('"v":1', '"v":1e0'),
  JSON.stringify({ ...hello(), v: 2 }),
  JSON.stringify({ ...hello(), v: '1' }),
  JSON.stringify({ ...hello(), v: true }),
  JSON.stringify({ type: 'hello', role: ROLE, nonce: nonceText }),
  JSON.stringify({ ...hello(), type: 'Hello' }),
  JSON.stringify({ ...hello(), x: 1 }),
  JSON.stringify({ v: 1, type: 'hello', role: ROLE }),
  JSON.stringify({ ...hello(), role: '' }),
  JSON.stringify({ ...hello(), role: `a${'b'.repeat(63)}` }),
  JSON.stringify({ ...hello(), role: `a${'b'.repeat(64)}` }),
  JSON.stringify({ ...hello(), role: 'Bad Role' }),
  JSON.stringify({ ...hello(), role: '1abc' }),
  JSON.stringify({ ...hello(), role: 'abc\n' }),
  JSON.stringify({ ...hello(), role: 'abé' }),
  JSON.stringify({ ...hello(), role: 'a.b_c:d-e9' }),
  JSON.stringify({ ...hello(), role: 1 }),
  JSON.stringify({ ...hello(), nonce: nonceText.slice(1) }),
  JSON.stringify({ ...hello(), nonce: `${nonceText}A` }),
  JSON.stringify({ ...hello(), nonce: `${nonceText.slice(1)}=` }),
  JSON.stringify({ ...hello(), nonce: `${nonceText.slice(1)}+` }),
  JSON.stringify({ ...hello(), nonce: `${nonceText.slice(1)}/` }),
  JSON.stringify({ ...hello(), nonce: `${nonceText}\n` }),
  JSON.stringify({ ...hello(), nonce: `${nonceText.slice(1)}é` }),
  `{"v":2,"v":1,"type":"hello","role":"${ROLE}","nonce":"${nonceText}"}`,
  `{"v":1,"v":2,"type":"hello","role":"${ROLE}","nonce":"${nonceText}"}`,
  '[]',
  'null',
  '"hello"',
  '1',
];
const challengeTexts = [
  JSON.stringify(challengeFor(sn, goodServerProof)),
  JSON.stringify({ v: 1, type: 'challenge', nonce: sn }),
  JSON.stringify({ ...challengeFor(sn, goodServerProof), x: 1 }),
  JSON.stringify(challengeFor(sn, goodServerProof.slice(1))),
  JSON.stringify(challengeFor(sn.slice(1), goodServerProof)),
  JSON.stringify({ ...challengeFor(sn, goodServerProof), v: 2 }),
  JSON.stringify({ ...challengeFor(sn, goodServerProof), type: 'proof' }),
];
const proofTexts = [
  JSON.stringify(proofFor(goodClientProof)),
  JSON.stringify({ ...proofFor(goodClientProof), nonce: cn }),
  JSON.stringify(proofFor(`${goodClientProof}A`)),
  JSON.stringify({ ...proofFor(goodClientProof), type: 'challenge' }),
  JSON.stringify({ v: 1, type: 'proof' }),
];
const acceptTexts = [
  '{"v":1,"type":"accept"}',
  '{"type":"accept","v":1}',
  '{"v":1,"type":"accept","x":1}',
  '{"v":2,"type":"accept"}',
  '{"v":1,"type":"acept"}',
  '{}',
];

const emoji = '\u{1F600}';
const rpcTexts = [
  '{"jsonrpc":"2.0","id":1,"method":"a.b","params":{"x":1}}',
  '{"jsonrpc":"2.0","id":1,"method":"a.b"}',
  '{"jsonrpc":"2.0","id":"abc","method":"a.b"}',
  '{"jsonrpc":"2.0","id":"","method":"a.b"}',
  `{"jsonrpc":"2.0","id":"${'x'.repeat(128)}","method":"a.b"}`,
  `{"jsonrpc":"2.0","id":"${'x'.repeat(129)}","method":"a.b"}`,
  `{"jsonrpc":"2.0","id":"${emoji.repeat(100)}","method":"a.b"}`,
  `{"jsonrpc":"2.0","id":"${emoji.repeat(129)}","method":"a.b"}`,
  '{"jsonrpc":"2.0","id":1.0,"method":"a.b"}',
  '{"jsonrpc":"2.0","id":1e2,"method":"a.b"}',
  '{"jsonrpc":"2.0","id":1.5,"method":"a.b"}',
  '{"jsonrpc":"2.0","id":9007199254740991,"method":"a.b"}',
  '{"jsonrpc":"2.0","id":9007199254740992,"method":"a.b"}',
  '{"jsonrpc":"2.0","id":-9007199254740991,"method":"a.b"}',
  '{"jsonrpc":"2.0","id":-9007199254740992,"method":"a.b"}',
  '{"jsonrpc":"2.0","id":null,"method":"a.b"}',
  '{"jsonrpc":"2.0","id":true,"method":"a.b"}',
  '{"jsonrpc":"2.0","id":[1],"method":"a.b"}',
  '{"jsonrpc":"2.0","id":1,"method":"Bad Name"}',
  '{"jsonrpc":"2.0","id":1,"method":"nodot"}',
  '{"jsonrpc":"2.0","id":1,"method":"a.b.c"}',
  '{"jsonrpc":"2.0","id":1,"method":"a._b"}',
  '{"jsonrpc":"2.0","id":1,"method":"a.b_1"}',
  '{"jsonrpc":"2.0","id":1,"method":"a.b\\n"}',
  '{"jsonrpc":"2.0","id":1,"method":"a.b."}',
  '{"jsonrpc":"2.0","id":1,"method":"rpc.cancel"}',
  '{"jsonrpc":"2.0","id":1,"method":"a.b","params":[]}',
  '{"jsonrpc":"2.0","id":1,"method":"a.b","params":null}',
  '{"jsonrpc":"2.0","id":1,"method":"a.b","params":"x"}',
  '{"jsonrpc":"2.0","id":1,"method":"a.b","params":{}}',
  '{"jsonrpc":"2.0","id":1,"method":"a.b","params":{"a\\nb":1,"":2}}',
  '{"jsonrpc":"1.0","id":1,"method":"a.b"}',
  '{"jsonrpc":2.0,"id":1,"method":"a.b"}',
  '{"id":1,"method":"a.b"}',
  '{"jsonrpc":"2.0","id":1,"method":"a.b","x":1}',
  '{"jsonrpc":"2.0","method":"a.b","params":{"seq":7}}',
  '{"jsonrpc":"2.0","method":"a.b"}',
  '{"jsonrpc":"2.0","method":"a.b","params":[]}',
  '{"jsonrpc":"2.0","method":"rpc.cancel","params":{"id":1}}',
  '{"jsonrpc":"2.0","id":1,"result":null}',
  '{"jsonrpc":"2.0","id":1,"result":{"a":[1,2]}}',
  '{"jsonrpc":"2.0","id":1}',
  '{"jsonrpc":"2.0","id":1,"result":1,"error":{"code":1,"message":"m"}}',
  '{"jsonrpc":"2.0","id":null,"result":1}',
  '{"jsonrpc":"2.0","id":"x","result":1}',
  '{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"m"}}',
  '{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"Invalid Request"}}',
  '{"jsonrpc":"2.0","id":1,"error":{"code":1,"message":"m","data":{"k":[1]}}}',
  '{"jsonrpc":"2.0","id":1,"error":{"code":1,"message":"m","data":null}}',
  '{"jsonrpc":"2.0","id":1,"error":{"code":1,"message":"m","x":1}}',
  '{"jsonrpc":"2.0","id":1,"error":{"code":1.5,"message":"m"}}',
  '{"jsonrpc":"2.0","id":1,"error":{"code":1.0,"message":"m"}}',
  '{"jsonrpc":"2.0","id":1,"error":{"code":1e300,"message":"m"}}',
  '{"jsonrpc":"2.0","id":1,"error":{"code":"1","message":"m"}}',
  '{"jsonrpc":"2.0","id":1,"error":{"code":1}}',
  '{"jsonrpc":"2.0","id":1,"error":{"code":1,"message":1}}',
  '{"jsonrpc":"2.0","id":1,"error":null}',
  '{"jsonrpc":"2.0","error":{"code":1,"message":"m"}}',
  '[]',
  '"x"',
  'null',
  '1',
];
const cancelTexts = [
  '{"id":1}',
  '{"id":"x"}',
  '{"id":null}',
  '{}',
  '{"id":1,"x":2}',
  '{"id":1.5}',
  '[]',
];

// ---------------------------------------------------------------------------
// 输出
// ---------------------------------------------------------------------------
const output = {
  source: {
    repository: 'leafliber/bellis',
    commit,
    files: [
      'packages/channel/src/frame.ts',
      'packages/channel/src/connection.ts',
      'packages/channel/src/address.ts',
      'packages/channel/src/endpoint.ts',
      'packages/channel/src/rpc.ts',
      'packages/protocol/src/channel.ts',
      'packages/protocol/src/jsonrpc.ts',
    ],
    generator: 'core/crates/bellis-channel/testdata/generate-vectors.ts',
    node: process.version,
  },
  constants,
  frames: frameVectors,
  decoder: decoderVectors,
  address: addressVectors,
  session: await session(),
  handshake_cases: [
    ...(await (async () => {
      const results = [];
      for (const [name, spec] of serverCases) results.push(await runCase('server', name, spec));
      for (const [name, spec] of clientCases) results.push(await runCase('client', name, spec));
      return results;
    })()),
  ],
  payloads: await (async () => {
    const results = [];
    for (const [name, payload] of payloadCases) results.push(await payloadCase(name, payload));
    return results;
  })(),
  messages: {
    hello: helloTexts.map((text) => ({ json: text, valid: isHello(text) })),
    challenge: challengeTexts.map((text) => ({ json: text, valid: isChallenge(text) })),
    proof: proofTexts.map((text) => ({ json: text, valid: isProof(text) })),
    accept: acceptTexts.map((text) => ({ json: text, valid: isAccept(text) })),
    rpc: rpcTexts.map((text) => ({ json: text, kind: classify(text) })),
    cancel_params: cancelTexts.map((text) => ({ json: text, valid: isCancel(text) })),
  },
};

process.stdout.write(`${JSON.stringify(output, null, 2)}\n`);
