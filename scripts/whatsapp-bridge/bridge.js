#!/usr/bin/env node
/**
 * Hermes Agent WhatsApp Bridge
 *
 * Standalone Node.js process that connects to WhatsApp via Baileys
 * and exposes HTTP endpoints for the Python gateway adapter.
 *
 * Endpoints (matches gateway/platforms/whatsapp.py expectations):
 *   GET  /messages       - Long-poll for new incoming messages
 *   POST /send           - Send a message { chatId, message, replyTo?, mentions? }
 *   POST /edit           - Edit a sent message { chatId, messageId, message }
 *   POST /send-media     - Send media natively { chatId, filePath, mediaType?, caption?, fileName?, mentions? }
 *   POST /send-location  - Send location pin { chatId, latitude, longitude, name?, address? }
 *   POST /typing         - Send typing indicator { chatId }
 *   GET  /chat/:id       - Get chat info
 *   GET  /health         - Health check
 *
 * Usage:
 *   node bridge.js --port 3000 --session ~/.hermes/whatsapp/session
 */

import { makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion, downloadMediaMessage, getAggregateVotesInPollMessage, decryptPollVote, getKeyAuthor, jidNormalizedUser } from '@whiskeysockets/baileys';
import express from 'express';
import { Boom } from '@hapi/boom';
import pino from 'pino';
import path from 'path';
import { mkdirSync, readFileSync, existsSync, readdirSync, unlinkSync, writeFileSync, renameSync } from 'fs';
import { fileURLToPath } from 'url';
import { randomBytes, createHash } from 'crypto';
import { execFileSync } from 'child_process';
import { tmpdir } from 'os';
import qrcode from 'qrcode-terminal';
import { createInterface } from 'node:readline/promises';
import { attachPasskeyFlow, loadPasskeySigner } from './passkey_socket.js';
import { matchesAllowedSender, matchesAllowedUser, matchesInboundWhatsAppGroup, parseAllowedUsers } from './allowlist.js';
import { createOutboundIdTracker } from './outbound_ids.js';
import { createMutedChatsGate } from './muted_gate.js';
import { classifyOwnerMessageGate } from './owner_message_gate.js';
import { bridgeAuthProof, createBridgeAuthMiddleware } from './bridge_auth.js';
import { esOptOutDeterministico } from './optout_gate.js';
import {
  parseTakeoverConfig,
  isTakeoverActive,
  isTakeoverActiveForTarget,
  markTakeover,
  pruneExpiredTakeovers,
} from './takeover_gate.js';
import {
  addMentions,
  buildPollPayload,
  createReconnectScheduler,
  createVersionResolver,
  installConsoleStamps,
  buildLocationPayload,
  buildTextSendPayload,
  createBoundedMessageStore,
  createInboundTextTracker,
  createQuotedMediaCache,
  deliveryStatusEvent,
  extractBridgeEvent,
  getMessageContent,
  inboundDedupeKey,
  inboundTextDedupeKey,
  inboundReadReceiptKeys,
  inferMediaType,
  lidMappingEntry,
  mediaPayloadForFile,
  normalizeWhatsAppId,
  pollCreationMessageFromPayload,
  pollUpdateForAggregation,
  resolveInboundSenderPhone,
  writeJsonLine,
} from './bridge_helpers.js';

// First statement: helpers capture console.log as a default at call time below.
installConsoleStamps();

// Parse CLI args
const args = process.argv.slice(2);
function getArg(name, defaultVal) {
  const idx = args.indexOf(`--${name}`);
  return idx !== -1 && args[idx + 1] ? args[idx + 1] : defaultVal;
}

const WHATSAPP_DEBUG =
  typeof process !== 'undefined' &&
  process.env &&
  typeof process.env.WHATSAPP_DEBUG === 'string' &&
  ['1', 'true', 'yes', 'on'].includes(process.env.WHATSAPP_DEBUG.toLowerCase());

// Opt-in: when true (and WHATSAPP_MODE === 'bot'), fromMe inbound messages
// that are NOT echoes of our own /send or /send-media calls are forwarded
// to the Python adapter with `fromOwner: true`. This lets plugins detect
// "owner just typed in this customer chat" — needed for handover / sliding
// TTL flows. Default OFF: existing deployments see no behavior change.
//
// Heuristic limitation: we distinguish bot-API-sent from owner-typed by
// looking up `key.id` in `recentlySentIds` (populated when /send returns).
// On bridge restart that set is empty, so a few in-flight bot replies may
// briefly look like owner-typed until they age out. Acceptable; we don't
// persist the set.
const FORWARD_OWNER_MESSAGES =
  typeof process !== 'undefined' &&
  process.env &&
  typeof process.env.WHATSAPP_FORWARD_OWNER_MESSAGES === 'string' &&
  ['1', 'true', 'yes', 'on'].includes(process.env.WHATSAPP_FORWARD_OWNER_MESSAGES.toLowerCase());

const SEND_READ_RECEIPTS =
  typeof process !== 'undefined' &&
  process.env &&
  typeof process.env.WHATSAPP_SEND_READ_RECEIPTS === 'string' &&
  ['1', 'true', 'yes', 'on'].includes(process.env.WHATSAPP_SEND_READ_RECEIPTS.toLowerCase());

const PORT = parseInt(getArg('port', '3000'), 10);
const SESSION_DIR = getArg('session', path.join(process.env.HOME || '~', '.hermes', 'whatsapp', 'session'));

// ── Handoff humano (takeover) ─────────────────────────────────────────
// When the owner/co-admin types in a patient chat from their phone, the bot
// must stop answering in that chat until TTL of owner silence elapses.
// State persists in SESSION_DIR/takeover.json so it survives bridge restarts.
const TAKEOVER_CFG = parseTakeoverConfig(process.env);
const TAKEOVER_FILE = path.join(SESSION_DIR, 'takeover.json');

// ── Chat silenciado por un humano (muted_chats.json) ──────────────────
// El gateway descarta los mensajes ENTRANTES de un chat silenciado; esto aplica
// la misma política a la SALIDA: si el chat está silenciado y la ventana sigue
// viva, el bridge NO manda y responde {skipped:true, reason:'chat_muted'}.
const mutedChatsGate = createMutedChatsGate({ sessionDir: SESSION_DIR });

function loadTakeoverState() {
  try {
    if (!existsSync(TAKEOVER_FILE)) return {};
    const raw = JSON.parse(readFileSync(TAKEOVER_FILE, 'utf8'));
    return pruneExpiredTakeovers(raw, Date.now());
  } catch {
    return {};
  }
}

let takeoverState = loadTakeoverState();

function persistTakeoverState() {
  try {
    takeoverState = pruneExpiredTakeovers(takeoverState, Date.now());
    writeFileSync(`${TAKEOVER_FILE}.tmp`, JSON.stringify(takeoverState, null, 2));
    renameSync(`${TAKEOVER_FILE}.tmp`, TAKEOVER_FILE);
  } catch (err) {
    console.warn('[bridge] takeover persist failed:', err.message);
  }
}

/** Every jid that identifies the same person (phone <-> lid) in this chat. */
function takeoverAliases(jid) {
  const out = new Set();
  const raw = String(jid || '');
  out.add(raw);
  out.add(raw.replace(/@.*/, '').replace(/:.*$/, ''));
  const num = raw.replace(/@.*/, '').replace(/:.*$/, '');
  // lid -> phone
  const mapped = (lidToPhone || {})[num];
  if (mapped) out.add(String(mapped).replace(/\D/g, ''));
  // phone -> lid (reverse index)
  for (const [lid, phone] of Object.entries(lidToPhone || {})) {
    if (String(phone).replace(/\D/g, '') === num) {
      out.add(lid);
      out.add(`${lid}@lid`);
    }
  }
  return out;
}

function isUnderOwnerTakeover(chatId) {
  if (!TAKEOVER_CFG.enabled) return false;
  return isTakeoverActiveForTarget(takeoverState, chatId, Date.now(), takeoverAliases);
}

// Cache directories: the Python gateway passes the profile-aware paths via
// env (HERMES_HOME-aware, new cache/ layout).  Fall back to the legacy
// hardcoded locations for bridges launched outside the gateway.
const IMAGE_CACHE_DIR = process.env.HERMES_IMAGE_CACHE_DIR
  || path.join(process.env.HOME || '~', '.hermes', 'image_cache');
const DOCUMENT_CACHE_DIR = process.env.HERMES_DOCUMENT_CACHE_DIR
  || path.join(process.env.HOME || '~', '.hermes', 'document_cache');
const AUDIO_CACHE_DIR = process.env.HERMES_AUDIO_CACHE_DIR
  || path.join(process.env.HOME || '~', '.hermes', 'audio_cache');

// Self-hash of this script file.  Reported in /health so the Python gateway
// can detect a running bridge that predates the current bridge.js and
// restart it instead of silently reusing stale code (stale-bridge trap:
// `hermes update` updates bridge.js on disk but a long-lived bridge process
// keeps serving the old behavior forever).
let SCRIPT_HASH = '';
try {
  // bridge_auth.js is in the hash: an adapter must never adopt a bridge whose auth code is stale.
  const entryPath = fileURLToPath(import.meta.url);
  const digest = createHash('sha256');
  for (const sourcePath of [entryPath, path.join(path.dirname(entryPath), 'bridge_auth.js')]) {
    digest.update(path.basename(sourcePath));
    digest.update('\0');
    digest.update(readFileSync(sourcePath));
    digest.update('\0');
  }
  SCRIPT_HASH = digest.digest('hex').slice(0, 16);
} catch {}
const PAIR_ONLY = args.includes('--pair-only');
// Exit status the gateway adapter reads as a non-retryable "pair with a passkey first" (PASSKEY_REQUIRED_EXIT in adapter.py).
const PASSKEY_REQUIRED_EXIT = 78;
const PAIR_JSON = args.includes('--pair-json');
const PASSKEY_BROWSER = getArg('passkey-browser', '');
let passkeySigner;
try {
  passkeySigner = await loadPasskeySigner(PASSKEY_BROWSER, {
    pairOnly: PAIR_ONLY, pairJson: PAIR_JSON, isTTY: process.stdin.isTTY,
  });
} catch (error) {
  console.error(error.message);
  process.exit(1);
}
// Per-session secret the adapter spawned us with. Loopback binding does not identify the caller:
// any local process can POST /send on this port, so every API route demands this bearer and
// /auth-proof lets the adapter verify a listener knows the secret before handing it over.
const BRIDGE_TOKEN = process.env.HERMES_WHATSAPP_BRIDGE_TOKEN || '';
const WHATSAPP_MODE = getArg('mode', process.env.WHATSAPP_MODE || 'self-chat'); // "bot" or "self-chat"
const WHATSAPP_DM_POLICY = String(process.env.WHATSAPP_DM_POLICY || 'open').trim().toLowerCase();
const WHATSAPP_GROUP_POLICY = String(process.env.WHATSAPP_GROUP_POLICY || 'pairing').trim().toLowerCase();
const ALLOWED_USERS = parseAllowedUsers(process.env.WHATSAPP_ALLOWED_USERS || '');
// Group authorization is by group JID, not by every participant's JID.  The
// Python adapter still applies group policy and mention rules after intake.
const GROUP_ALLOWED_USERS = parseAllowedUsers(process.env.WHATSAPP_GROUP_ALLOWED_USERS || '');
const DEFAULT_REPLY_PREFIX = '☤ *Hermes Agent*\n────────────\n';
const REPLY_PREFIX = process.env.WHATSAPP_REPLY_PREFIX === undefined
  ? DEFAULT_REPLY_PREFIX
  : process.env.WHATSAPP_REPLY_PREFIX.replace(/\\n/g, '\n');
const MAX_MESSAGE_LENGTH = parseInt(process.env.WHATSAPP_MAX_MESSAGE_LENGTH || '4096', 10);
const CHUNK_DELAY_MS = parseInt(process.env.WHATSAPP_CHUNK_DELAY_MS || '300', 10);
// Per-call timeout for sock.sendMessage(). Baileys occasionally hangs forever
// when uploading media to WhatsApp servers (and, less often, on text sends),
// which pins the bridge's HTTP handler until the upstream aiohttp timeout
// fires. Fail fast instead so the gateway can surface a real error and retry.
const SEND_TIMEOUT_MS = parseInt(process.env.WHATSAPP_SEND_TIMEOUT_MS || '60000', 10);

// --- Send queue: serialise all sock.sendMessage() calls across concurrent
//     HTTP handlers so a single Baileys socket never has overlapping sends.
//     Overlapping sends are the root cause of cross-chat contamination
//     (#33360) — the WhatsApp protocol-level routing can misdeliver when
//     two sendMessage() Promises race on the same socket. ---
let _sendQueue = Promise.resolve();

function enqueueSend(fn) {
  const task = _sendQueue.then(() => fn(), () => fn());
  _sendQueue = task.catch(() => {});
  return task;
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function sendWithTimeout(chatId, payload, options = {}, timeoutMs = SEND_TIMEOUT_MS) {
  let timer;
  const timeoutPromise = new Promise((_, reject) => {
    timer = setTimeout(
      () => reject(new Error(`sendMessage timed out after ${timeoutMs / 1000}s`)),
      timeoutMs,
    );
  });
  return enqueueSend(() =>
    Promise.race([sock.sendMessage(chatId, payload, options), timeoutPromise])
      .finally(() => clearTimeout(timer))
  );
}

function formatOutgoingMessage(message) {
  // In bot mode, messages come from a different number so the prefix is
  // redundant — the sender identity is already clear.  Only prepend in
  // self-chat mode where bot and user share the same number.
  if (WHATSAPP_MODE !== 'self-chat') return message;
  return REPLY_PREFIX ? `${REPLY_PREFIX}${message}` : message;
}

function splitLongMessage(message, maxLength = MAX_MESSAGE_LENGTH) {
  const text = String(message || '');
  if (!text) return [];
  if (!Number.isFinite(maxLength) || maxLength < 1 || text.length <= maxLength) {
    return [text];
  }

  const chunks = [];
  let remaining = text;
  while (remaining.length > maxLength) {
    let splitAt = remaining.lastIndexOf('\n', maxLength);
    if (splitAt < Math.floor(maxLength / 2)) {
      splitAt = remaining.lastIndexOf(' ', maxLength);
    }
    if (splitAt < 1) splitAt = maxLength;

    chunks.push(remaining.slice(0, splitAt).trimEnd());
    remaining = remaining.slice(splitAt).trimStart();
  }
  if (remaining) chunks.push(remaining);
  return chunks;
}

function rememberSentMessage(sent, payload) {
  if (!sent?.key?.id) return;
  if (sent.message) {
    messageStore.remember(sent);
    return;
  }
  const syntheticMessage = pollCreationMessageFromPayload(payload);
  if (syntheticMessage) {
    messageStore.remember({ ...sent, message: syntheticMessage });
  }
}

function trackSentMessageId(sent) {
  rememberSentId(sent?.key?.id);
}

function redactWhatsAppId(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  const [userPart, domainPart = ''] = raw.split('@', 2);
  const bare = userPart.split(':', 1)[0];
  const digits = bare.replace(/\D/g, '');
  const suffix = digits ? digits.slice(-4) : bare.slice(-4);
  return `${suffix ? `…${suffix}` : '…'}${domainPart ? `@${domainPart}` : ''}`;
}

// ── Interceptor determinístico de BAJA (opt-out) ──────────────────────
// Un pedido de baja de comunicaciones no llega al LLM: se responde con texto
// fijo, se avisa al backend (best-effort) y el evento NO entra a messageQueue.
// Clasificador: ./optout_gate.js. Spec: PLANO-MOTOR-REACTIVACION §5.2.
const OPTOUT_RESPUESTA_FIJA =
  'Listo, no te escribimos más. Si algún día necesitás algo, escribinos vos.';
const OPTOUT_EVENTO_URL = process.env.HERMES_OPTOUT_EVENTO_URL
  || 'http://127.0.0.1:8000/api/bot/reactivacion/evento-entrante/';
const OPTOUT_EVENTO_TIMEOUT_MS = 3000;

function maskOptOutPhone(telefono) {
  const digits = String(telefono || '').replace(/\D/g, '');
  return digits ? `…${digits.slice(-4)}` : '(sin numero)';
}

// Fire-and-forget: la respuesta fija ya salió; si el endpoint no existe todavía
// (404 hoy) o el backend está caído, se loguea y el bridge sigue vivo.
function notificarOptOutBackend({ telefono, texto, messageId }) {
  if (!telefono) {
    console.warn('[bridge] optout_backend_omitido motivo=telefono_no_resuelto');
    return;
  }
  const apiKey = process.env.ADEN_BOT_API_KEY || '';
  if (!apiKey) {
    console.warn('[bridge] optout_backend_omitido motivo=falta_ADEN_BOT_API_KEY');
    return;
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), OPTOUT_EVENTO_TIMEOUT_MS);
  fetch(OPTOUT_EVENTO_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify({
      telefono,
      texto,
      es_baja_deterministica: true,
      message_id_entrante: messageId || '',
    }),
    redirect: 'manual',
    signal: controller.signal,
  })
    .then(res => {
      if (!res.ok) {
        console.warn(`[bridge] optout_backend_http telefono=${maskOptOutPhone(telefono)} status=${res.status}`);
      }
    })
    .catch(err => {
      console.warn(`[bridge] optout_backend_error telefono=${maskOptOutPhone(telefono)}:`, err.message);
    })
    .finally(() => clearTimeout(timer));
}

function emitDebugEvent(payload) {
  if (!WHATSAPP_DEBUG) return;
  try {
    writeJsonLine({ event: 'debug', ...payload });
  } catch {}
}

mkdirSync(SESSION_DIR, { recursive: true });

// Build LID → phone reverse map from session files.  Baileys writes both
// `lid-mapping-<phone>.json` (contents = LID) and, for first contacts that only
// arrived by LID, `lid-mapping-<lid>_reverse.json` (contents = phone); reading
// only the first shape left real senders unresolved.
function buildLidMap() {
  const map = {};
  try {
    for (const f of readdirSync(SESSION_DIR)) {
      if (!/^lid-mapping-\d+(_reverse)?\.json$/.test(f)) continue;
      const entry = lidMappingEntry(f, JSON.parse(readFileSync(path.join(SESSION_DIR, f), 'utf8')));
      if (entry) map[entry[0]] = entry[1];
    }
  } catch {}
  return map;
}
let lidToPhone = buildLidMap();

const logger = pino({ level: 'warn' });

// Message queue for polling
const messageQueue = [];
const MAX_QUEUE_SIZE = 100;

// Track recently sent message IDs.  Two purposes:
//   1. Prevent echo-back loops with media in self-chat mode.
//   2. (When WHATSAPP_FORWARD_OWNER_MESSAGES=true) distinguish our own
//      bot-API outbound messages from owner-typed messages on the linked
//      device so we can forward only the latter.
// Capacity bounded (see outbound_ids.js) to keep memory flat under
// sustained sending.
const recentlySentIds = createOutboundIdTracker(512);
const recentlyProcessedPollUpdates = createOutboundIdTracker(512);
// Inbound de-duplication.  Baileys can emit the same incoming message twice
// ('append' then 'notify', or a retransmission after a lost ack).  Each copy
// used to be queued and reached Python as its own MessageEvent, so the agent
// ran one turn per copy and answered the customer twice.  Remember
// "chatId:messageId" and drop the repeat before it reaches the queue.
const recentlyProcessedInbound = createOutboundIdTracker(1024);
// Second inbound guard, by CONTENT.  WhatsApp retransmits the same customer
// send ~2 s later under a different message id and a different native type
// ('conversation' vs 'extendedTextMessage'), so the id tracker above cannot
// see that pair.  Same chat + same normalized text inside 6 s is one message;
// media and empty bodies are never keyed.
const INBOUND_TEXT_DEDUPE_WINDOW_MS = 6000;
const recentlyProcessedInboundText = createInboundTextTracker({
  windowMs: INBOUND_TEXT_DEDUPE_WINDOW_MS,
  maxSize: 1024,
});
const messageStore = createBoundedMessageStore(512);
// Bounded cache of already-downloaded inbound media, so a later reply to an
// uncaptioned photo/video/document/voice note can still surface the original
// file — see createQuotedMediaCache's doc comment in bridge_helpers.js.
const quotedMediaCache = createQuotedMediaCache(512);

function normalizePollUpdateOptions(aggregation, pollUpdateMessage, meId) {
  const selected = [];
  for (const option of aggregation || []) {
    if ((option.voters || []).length > 0 && option.name && option.name !== 'Unknown') {
      selected.push(option.name);
    }
  }
  if (selected.length > 0) return selected;

  // Fallback for already-decrypted pollUpdateMessage payloads where Baileys did
  // not have the creation message available. This may only yield hashes, but
  // keeping them in metadata is still better than dropping the vote entirely.
  const raw = pollUpdateMessage?.vote?.selectedOptions || [];
  return raw.map(option => String(option)).filter(Boolean);
}

function pollAggregationSummary(aggregation) {
  return (aggregation || []).map(option => ({
    name: option?.name || '',
    voterCount: (option?.voters || []).length,
  }));
}

function logPollUpdateDiagnostic({ sourcePath, pollId, pollCreation, pollUpdates, selectedOptions, aggregation }) {
  const firstUpdate = pollUpdates?.[0] || {};
  try {
    writeJsonLine({
      event: 'poll_update_decode',
      sourcePath,
      pollId: pollId || '',
      pollCreationFound: !!pollCreation,
      updateKeys: Object.keys(firstUpdate),
      hasVote: !!firstUpdate.vote,
      selectedOptionsLength: selectedOptions?.length || 0,
      aggregation: pollAggregationSummary(aggregation),
    });
  } catch {}
}

function enqueuePollUpdateEvent({ key, update, selectedOptions, aggregation }) {
  const chatId = normalizeWhatsAppId(key?.remoteJid || update?.pollUpdates?.[0]?.pollUpdateMessageKey?.remoteJid || '');
  const senderId = normalizeWhatsAppId(
    key?.participant
    || update?.pollUpdates?.[0]?.pollUpdateMessageKey?.participant
    || chatId
  );
  const pollId = key?.id
    || update?.pollUpdates?.[0]?.pollCreationMessageKey?.id
    || update?.pollUpdates?.[0]?.pollUpdateMessageKey?.id
    || '';
  // Only surface votes on polls Hermes itself created (tracked when
  // /send-poll returns). Arbitrary human polls in a group chat must not
  // inject agent-visible messages on every vote.
  if (!pollId || !recentlySentIds.has(pollId)) {
    if (WHATSAPP_DEBUG) {
      try { writeJsonLine({ event: 'ignored', reason: 'foreign_poll_update', pollId }); } catch {}
    }
    return;
  }
  const chosenText = selectedOptions.length ? selectedOptions.join(', ') : `[Poll update${pollId ? `: ${pollId}` : ''}]`;
  const dedupeId = `poll:${pollId}:${senderId}:${selectedOptions.join('|')}`;
  if (recentlyProcessedPollUpdates.has(dedupeId)) return;
  recentlyProcessedPollUpdates.remember(dedupeId);
  const event = {
    messageId: `${pollId || 'poll'}:update:${Date.now()}`,
    chatId,
    senderId,
    senderName: senderId.replace(/@.*/, ''),
    chatName: chatId.replace(/@.*/, ''),
    isGroup: chatId.endsWith('@g.us'),
    body: chosenText,
    hasMedia: false,
    mediaType: 'poll_update',
    mime: '',
    fileName: '',
    nativeType: 'pollUpdateMessage',
    nativeMetadata: {
      pollUpdate: {
        pollId,
        selectedOptions,
        aggregation,
      },
    },
    mediaUrls: [],
    mentionedIds: [],
    quotedMessageId: pollId,
    quotedParticipant: '',
    quotedRemoteJid: chatId,
    quotedText: '',
    hasQuotedMessage: !!pollId,
    botIds: [],
    timestamp: Math.floor(Date.now() / 1000),
  };
  messageQueue.push(event);
  if (messageQueue.length > MAX_QUEUE_SIZE) {
    messageQueue.shift();
  }
}

function rememberSentId(id) {
  recentlySentIds.remember(id);
}

// Receipts get their own bound: one reply to a large group brings a receipt per member, and in the
// shared queue that burst evicted inbound messages still waiting for the next poll.
const deliveryQueue = [];

function enqueueDeliveryStatus(args) {
  const event = deliveryStatusEvent({ ...args, isOwnSend: id => recentlySentIds.has(id) });
  if (!event) return;
  deliveryQueue.push(event);
  if (deliveryQueue.length > MAX_QUEUE_SIZE) {
    deliveryQueue.shift();
  }
}

let sock = null;
let connectionState = 'disconnected';

function emitPairEvent(event) {
  if (!PAIR_JSON) return;
  try {
    writeJsonLine({ ts: Date.now(), ...event });
  } catch {}
}

const scheduleReconnect = createReconnectScheduler(() => startSocket());
const getWAVersion = createVersionResolver(fetchLatestBaileysVersion);

async function confirmPasskeyCode(code, signal) {
  const terminal = createInterface({ input: process.stdin, output: process.stdout });
  try {
    const answer = await terminal.question(`Does the code ${code} match your phone? [y/N] `, { signal });
    if (answer.trim().toLowerCase() !== 'y') throw new Error('Owner declined passkey confirmation');
  } finally {
    terminal.close();
  }
}

async function startSocket() {
  const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);
  const version = await getWAVersion();

  sock = makeWASocket({
    ...(version ? { version } : {}),
    auth: state,
    logger,
    printQRInTerminal: false,
    browser: ['Hermes Agent', 'Chrome', '120.0'],
    syncFullHistory: false,
    markOnlineOnConnect: false,
    // Required for Baileys 7.x: without this, incoming messages that need
    // E2EE session re-establishment are silently dropped (msg.message === null)
    getMessage: async (key) => {
      // We don't maintain a message store, so return a placeholder.
      // This is enough for Baileys to complete the retry handshake.
      return { conversation: '' };
    },
  });

  sock.ev.on('creds.update', () => { saveCreds(); lidToPhone = buildLidMap(); });

  attachPasskeyFlow(sock, { logger, signAssertion: passkeySigner, confirmCode: confirmPasskeyCode });
  sock.ev.on('connection.update', (update) => {
    if (update.passkeyRequired && passkeySigner) {
      if (!PAIR_JSON) console.log('Complete passkey verification in the browser window.');
    } else if (update.passkeyRequired && !PAIR_ONLY && (connectionState === 'connected' || state.creds.me?.id)) {
      // A paired gateway session keeps running, whether open or still logging in after a reconnect (rc13
      // acked and ignored this request before passkey support): exiting with PASSKEY_REQUIRED_EXIT would
      // park a working gateway as non-retryable. Pair-only still fails so setup never reports a pairing
      // the server questioned.
      console.warn('WhatsApp sent a passkey request to an already connected session; ignoring it.');
    } else if (update.passkeyRequired) {
      // The dashboard's pairing watcher only understands error events; no signer is ever loaded there.
      emitPairEvent({ event: 'error', error: 'passkey_required' });
      console.error('WhatsApp requires passkey verification. Pair with --passkey-browser and a local Chrome/Edge executable.');
      process.exit(PAIR_ONLY ? 1 : PASSKEY_REQUIRED_EXIT);
    }
    if (update.passkeyError) {
      // Name and message only: the cause (missing browser, cancelled prompt, declined code, failed query)
      // is what the user needs; assertion and credential bytes never reach the error text.
      const { name, message } = update.passkeyError;
      console.error(`WhatsApp passkey verification failed: ${name || 'Error'}: ${message || 'unknown error'}`);
      process.exit(1);
    }
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      if (PAIR_JSON) {
        emitPairEvent({ event: 'qr', qr });
      } else {
        console.log('\n📱 Scan this QR code with WhatsApp on your phone:\n');
        // The QR block is multi-line art; a stamp on its first row would skew it.
        qrcode.generate(qr, { small: true }, (code) => process.stdout.write(`${code}\n`));
        console.log('\nWaiting for scan...\n');
      }
    }

    if (connection === 'close') {
      const reason = new Boom(lastDisconnect?.error)?.output?.statusCode;
      connectionState = 'disconnected';

      if (reason === DisconnectReason.loggedOut) {
        emitPairEvent({ event: 'error', error: 'logged_out', reason });
        if (!PAIR_JSON) {
          console.log('❌ Logged out. Delete session and restart to re-authenticate.');
        }
        process.exit(1);
      } else {
        // 515 = restart requested (common after pairing). Always reconnect.
        emitPairEvent({ event: 'disconnected', reason });
        if (!PAIR_JSON) {
          if (reason === 515) {
            console.log('↻ WhatsApp requested restart (code 515). Reconnecting...');
          } else {
            console.log(`⚠️  Connection closed (reason: ${reason}). Reconnecting in 3s...`);
          }
        }
        scheduleReconnect(reason === 515 ? 1000 : 3000);
      }
    } else if (connection === 'open') {
      connectionState = 'connected';
      const connectedUser = sock?.user
        ? {
            id: sock.user.id || null,
            name: sock.user.name || sock.user.verifiedName || null,
          }
        : null;
      emitPairEvent({ event: 'connected', user: connectedUser });
      if (!PAIR_JSON) {
        console.log('✅ WhatsApp connected!');
      }
      if (PAIR_ONLY) {
        if (!PAIR_JSON) {
          console.log('✅ Pairing complete. Credentials saved.');
        }
        // Give Baileys a moment to flush creds, then exit cleanly
        setTimeout(() => process.exit(0), 2000);
      }
    }
  });

  sock.ev.on('message-receipt.update', (receipts) => {
    for (const { key, receipt } of receipts || []) enqueueDeliveryStatus({ key, receipt });
  });

  sock.ev.on('messages.update', async (updates) => {
    for (const { key, update } of updates || []) {
      if (update?.status !== undefined) enqueueDeliveryStatus({ key, update });
      if (!update?.pollUpdates) continue;
      const pollCreationId = key?.id || update.pollUpdates?.[0]?.pollCreationMessageKey?.id;
      const pollCreation = messageStore.get(pollCreationId);
      let aggregation = [];
      let pollUpdates = update.pollUpdates;
      try {
        if (pollCreation) {
          const meId = jidNormalizedUser(sock.user?.id || 'me');
          pollUpdates = update.pollUpdates.map(pollUpdate => (
            pollUpdateForAggregation({
              pollUpdateMessage: pollUpdate,
              pollUpdateMessageKey: pollUpdate.pollUpdateMessageKey,
              pollCreation,
              decryptPollVote,
              getKeyAuthor,
              meId,
              pollCreatorJids: [
                jidNormalizedUser(sock.user?.lid || ''),
                jidNormalizedUser(sock.user?.id || ''),
                getKeyAuthor(pollUpdate.pollCreationMessageKey || key, jidNormalizedUser(sock.user?.lid || '')),
                getKeyAuthor(pollUpdate.pollCreationMessageKey || key, jidNormalizedUser(sock.user?.id || '')),
              ],
              voterJids: [
                normalizeWhatsAppId(pollUpdate.pollUpdateMessageKey?.participant || ''),
                normalizeWhatsAppId(pollUpdate.pollUpdateMessageKey?.remoteJid || key?.remoteJid || ''),
              ],
            }) || pollUpdate
          ));
          aggregation = getAggregateVotesInPollMessage({
            message: pollCreation.message,
            pollUpdates,
          });
        }
      } catch (err) {
        console.warn('[bridge] failed to aggregate poll update:', err.message);
      }
      const selectedOptions = normalizePollUpdateOptions(aggregation, pollUpdates?.[0]);
      logPollUpdateDiagnostic({
        sourcePath: 'messages.update',
        pollId: pollCreationId,
        pollCreation,
        pollUpdates,
        selectedOptions,
        aggregation,
      });
      enqueuePollUpdateEvent({ key, update: { ...update, pollUpdates }, selectedOptions, aggregation });
    }
  });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    // In self-chat mode, your own messages commonly arrive as 'append' rather
    // than 'notify'. Accept both and filter agent echo-backs below.
    if (type !== 'notify' && type !== 'append') return;

    const botIds = Array.from(new Set([
      normalizeWhatsAppId(sock.user?.id),
      normalizeWhatsAppId(sock.user?.lid),
    ].filter(Boolean)));

    for (const msg of messages) {
      if (!msg.message) continue;

      const chatId = msg.key.remoteJid;
      const senderId = msg.key.participant || chatId;
      // Baileys v7 carries the other form of the sender here (group: key.participantAlt,
      // DM: key.remoteJidAlt). A LID sender's phone twin makes a phone allowlist match with no
      // lid-mapping file yet (#63415, #72529) and is the identity Python sees, so first
      // contacts key the same session they will once the mapping exists.
      const senderAltId = normalizeWhatsAppId(msg.key.participantAlt || msg.key.remoteJidAlt || '');
      const resolvedSenderId = senderAltId.endsWith('@s.whatsapp.net') ? senderAltId : senderId;
      const isGroup = chatId.endsWith('@g.us');
      const senderNumber = resolvedSenderId.replace(/@.*/, '');
      // Real phone of a DM sender: the phone twin Baileys puts on the envelope,
      // else the session's LID→phone map.  Empty for groups, fromMe and LIDs
      // with no mapping — the adapter only prefixes when this is present.
      const resolvePhone = () => resolveInboundSenderPhone({
        chatId,
        senderAltId,
        lidToPhone,
        isGroup,
        fromMe: !!msg.key.fromMe,
      });
      let senderPhone = resolvePhone();
      // A first contact's mapping file can land after the last creds.update, and
      // the in-memory map would then miss it for the life of the process.  One
      // refresh per unresolved LID is cheap next to losing the patient's phone.
      if (!senderPhone && !isGroup && !msg.key.fromMe && chatId.endsWith('@lid')) {
        lidToPhone = buildLidMap();
        senderPhone = resolvePhone();
      }
      emitDebugEvent({
        stage: 'upsert',
        type,
        fromMe: !!msg.key.fromMe,
        chatId: redactWhatsAppId(chatId),
        senderId: redactWhatsAppId(senderId),
        messageKeys: Object.keys(msg.message || {}),
      });

      // Handle fromMe messages based on mode
      let fromOwner = false;
      if (msg.key.fromMe) {
        if (isGroup || chatId.includes('status')) {
          emitDebugEvent({
            stage: 'ignored',
            reason: isGroup ? 'from_me_group' : 'from_me_status',
            chatId: redactWhatsAppId(chatId),
          });
          continue;
        }

        if (WHATSAPP_MODE === 'bot') {
          // Bot mode: separate bot number. fromMe inbound is either
          //   (a) an echo of our own /send (recentlySentIds will catch it), or
          //   (b) a message the owner typed from their own phone using the
          //       linked-device session.
          //
          // We always drop (a). We drop (b) too unless the operator opts in
          // via WHATSAPP_FORWARD_OWNER_MESSAGES so existing deployments see
          // no behavior change. When opted in, we still gate on the
          // customer chatId allowlist — without that gate, any contact
          // the owner replied to would leak into Hermes and trigger
          // implicit handover. See `owner_message_gate.js`.
          const decision = classifyOwnerMessageGate({
            fromMe: true,
            fromOwnerEnabled: FORWARD_OWNER_MESSAGES,
            recentlySent: recentlySentIds,
            allowlistMatches: (id) => matchesAllowedUser(id, ALLOWED_USERS, SESSION_DIR),
            messageId: msg.key.id,
            chatId,
          });
          if (decision.action === 'drop_echo') continue;
          if (decision.action === 'drop_disabled') continue;
          if (decision.action === 'drop_allowlist') {
            try {
              writeJsonLine({
                event: 'ignored',
                reason: 'allowlist_mismatch_owner_chat',
                chatId,
                senderId,
              });
            } catch {}
            continue;
          }
          fromOwner = true;
          // Handoff humano: el dueño/co-admin escribió desde su teléfono en
          // este chat → el bot se calla acá por TTL rodante (2 h por defecto).
          // Sin esto el bot sigue contestando y pisa la indicación clínica.
          if (TAKEOVER_CFG.enabled) {
            takeoverState = markTakeover(
              takeoverState,
              chatId,
              Date.now(),
              TAKEOVER_CFG.ttlMs,
              '',
            );
            persistTakeoverState();
            emitDebugEvent({
              stage: 'owner_takeover',
              chatId: redactWhatsAppId(chatId),
              ttlHours: TAKEOVER_CFG.ttlMs / 3600_000,
            });
          }
        } else {
          // Self-chat mode: only allow messages in the user's own self-chat.
          // WhatsApp now uses LID (Linked Identity Device) format: 67427329167522@lid
          // AND classic format: 34652029134@s.whatsapp.net
          // sock.user has both: { id: "number:10@s.whatsapp.net", lid: "lid_number:10@lid" }
          const myNumber = (sock.user?.id || '').replace(/:.*@/, '@').replace(/@.*/, '');
          const myLid = (sock.user?.lid || '').replace(/:.*@/, '@').replace(/@.*/, '');
          const chatNumber = chatId.replace(/@.*/, '');
          const isSelfChat = (myNumber && chatNumber === myNumber) || (myLid && chatNumber === myLid);
          emitDebugEvent({
            stage: 'self_chat_check',
            matched: !!isSelfChat,
            chatId: redactWhatsAppId(chatId),
            accountId: redactWhatsAppId(sock.user?.id),
            accountLid: redactWhatsAppId(sock.user?.lid),
          });
          if (!isSelfChat) {
            emitDebugEvent({
              stage: 'ignored',
              reason: 'self_chat_mismatch',
              chatId: redactWhatsAppId(chatId),
              senderId: redactWhatsAppId(senderId),
            });
            continue;
          }
        }
      }

      // Handle !fromMe messages (from other people) based on mode.
      // Self-chat mode only responds to the user's own messages to
      // themselves — stranger DMs / group pings must never reach the
      // Python gateway, otherwise a pairing-code reply fires in response
      // to arbitrary incoming messages (#8389).
      if (!msg.key.fromMe) {
        if (WHATSAPP_MODE === 'self-chat') {
          try {
            writeJsonLine({
              event: 'ignored',
              reason: 'self_chat_mode_rejects_non_self',
              // Every other chat on the owner's own account lands here: full ids would turn
              // bridge.log into a contact and group list (#149860).
              chatId: redactWhatsAppId(chatId),
              senderId: redactWhatsAppId(senderId),
            });
          } catch {}
          continue;
        }
        const intakeAllowed = isGroup
          ? matchesInboundWhatsAppGroup({
              chatId,
              groupPolicy: WHATSAPP_GROUP_POLICY,
              groupAllowedUsers: GROUP_ALLOWED_USERS,
              sessionDir: SESSION_DIR,
            })
          : WHATSAPP_DM_POLICY === 'pairing'
            || matchesAllowedSender(senderId, senderAltId, ALLOWED_USERS, SESSION_DIR);
        if (!intakeAllowed) {
          try {
            writeJsonLine({
              event: 'ignored',
              reason: isGroup ? 'group_policy_rejected' : 'allowlist_mismatch',
              chatId,
              senderId,
              senderAltId,
            });
          } catch {}
          continue;
        }
      }

      const messageContent = getMessageContent(msg);
      if (messageContent.pollUpdateMessage) {
        const pollUpdateMessage = messageContent.pollUpdateMessage;
        const pollKey = pollUpdateMessage.pollCreationMessageKey || {
          id: pollUpdateMessage.key?.id || msg.key.id,
          remoteJid: chatId,
          participant: senderId,
        };
        const pollCreation = messageStore.get(pollKey.id);
        let aggregation = [];
        let pollUpdates = [pollUpdateMessage];
        try {
          if (pollCreation) {
            const meId = jidNormalizedUser(sock.user?.id || 'me');
            const pollUpdate = pollUpdateForAggregation({
              pollUpdateMessage,
              pollUpdateMessageKey: msg.key,
              pollCreation,
              decryptPollVote,
              getKeyAuthor,
              meId,
              pollCreatorJids: [
                jidNormalizedUser(sock.user?.lid || ''),
                jidNormalizedUser(sock.user?.id || ''),
                getKeyAuthor(pollUpdateMessage.pollCreationMessageKey || pollKey, jidNormalizedUser(sock.user?.lid || '')),
                getKeyAuthor(pollUpdateMessage.pollCreationMessageKey || pollKey, jidNormalizedUser(sock.user?.id || '')),
              ],
              voterJids: [
                normalizeWhatsAppId(msg.key?.participant || ''),
                normalizeWhatsAppId(msg.key?.remoteJid || chatId || ''),
                normalizeWhatsAppId(senderId || ''),
              ],
            });
            if (pollUpdate) pollUpdates = [pollUpdate];
            aggregation = getAggregateVotesInPollMessage({
              message: pollCreation.message,
              pollUpdates,
            });
          }
        } catch (err) {
          console.warn('[bridge] failed to aggregate poll upsert:', err.message);
        }
        const selectedOptions = normalizePollUpdateOptions(aggregation, pollUpdates[0]);
        logPollUpdateDiagnostic({
          sourcePath: 'messages.upsert',
          pollId: pollKey.id,
          pollCreation,
          pollUpdates,
          selectedOptions,
          aggregation,
        });
        enqueuePollUpdateEvent({
          key: { ...pollKey, remoteJid: pollKey.remoteJid || chatId, participant: pollKey.participant || senderId },
          update: { pollUpdates },
          selectedOptions,
          aggregation,
        });
        continue;
      }

      // Drop a repeat of an already-processed inbound message.  Baileys
      // delivers the same message under both 'append' and 'notify' (and
      // retransmits when an ack is lost); before this guard each copy became
      // its own MessageEvent and the agent ran one turn per copy, answering
      // the customer twice.  Keyed by chat + message id, so identical text
      // sent twice by the customer (different ids) is never dropped.
      // fromMe messages keep their own echo/owner handling above, and poll
      // updates returned before this point.
      const inboundKey = inboundDedupeKey({ chatId, messageId: msg.key.id, fromMe: !!msg.key.fromMe });
      if (inboundKey) {
        if (recentlyProcessedInbound.has(inboundKey)) {
          emitDebugEvent({
            stage: 'ignored',
            reason: 'duplicate_inbound',
            chatId: redactWhatsAppId(chatId),
            messageId: msg.key.id,
          });
          continue;
        }
        recentlyProcessedInbound.remember(inboundKey);
      }

      const event = await extractBridgeEvent({
        msg,
        chatId,
        senderId: resolvedSenderId,
        senderNumber,
        botIds,
        isGroup,
        downloadMedia: async (mediaMsg) => downloadMediaMessage(mediaMsg, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage }),
        cacheDirs: {
          image: IMAGE_CACHE_DIR,
          document: DOCUMENT_CACHE_DIR,
          audio: AUDIO_CACHE_DIR,
        },
        lookupQuotedMedia: (quotedChatId, quotedMessageId) => quotedMediaCache.get(quotedChatId, quotedMessageId),
      });
      event.fromOwner = fromOwner;
      event.senderPhone = senderPhone;

      // Ignore Hermes' own reply messages in self-chat mode to avoid loops.
      if (msg.key.fromMe && ((REPLY_PREFIX && event.body.startsWith(REPLY_PREFIX)) || recentlySentIds.has(msg.key.id))) {
        if (WHATSAPP_DEBUG) {
          emitDebugEvent({
            stage: 'ignored',
            reason: 'agent_echo',
            chatId: redactWhatsAppId(chatId),
            messageId: msg.key.id,
          });
        }
        continue;
      }

      // Una reacción NO es una pregunta: el bot la registra pero no contesta.
      // Antes generaba una respuesta por cada emoji (4 respuestas a 3
      // reacciones en el caso Fran), puro ruido en el chat del paciente.
      if (event.nativeType === 'reactionMessage') {
        emitDebugEvent({
          stage: 'ignored',
          reason: 'reaction_no_reply',
          chatId: redactWhatsAppId(chatId),
        });
        continue;
      }

      // Skip empty messages (but not a bare quote-reply whose own text/media
      // is empty when the quoted message resolved to cached media).
      if (!event.body && !event.hasMedia && !event.quotedMediaUrls.length) {
        emitDebugEvent({
          stage: 'ignored',
          reason: 'empty',
          chatId: redactWhatsAppId(chatId),
          messageKeys: Object.keys(msg.message || {}),
        });
        continue;
      }

      // Second layer: the same customer text re-delivered with a DIFFERENT
      // message id ('conversation' vs 'extendedTextMessage', ~2 s apart) is
      // invisible to the id guard above.  Keyed by chat + normalized text in a
      // 6 s window, before anything is queued or remembered.  Media, empty
      // bodies and fromMe events are never keyed, so attachments always pass.
      const inboundTextKey = inboundTextDedupeKey({
        chatId,
        body: event.body,
        hasMedia: event.hasMedia,
        mediaType: event.mediaType,
        mediaUrls: event.mediaUrls,
        quotedMediaUrls: event.quotedMediaUrls,
      });
      if (inboundTextKey) {
        if (recentlyProcessedInboundText.has(inboundTextKey)) {
          emitDebugEvent({
            stage: 'ignored',
            reason: 'duplicate_inbound_text',
            chatId: redactWhatsAppId(chatId),
            messageId: msg.key.id,
            bodyLength: event.body.length,
          });
          continue;
        }
        recentlyProcessedInboundText.remember(inboundTextKey);
      }

      messageStore.remember(msg);
      // Remember this message's already-downloaded media/text so a later
      // reply to it (even uncaptioned media) can resolve the original
      // content instead of seeing only a stripped-down quoted-message stub.
      quotedMediaCache.remember(chatId, msg.key.id, {
        body: event.body,
        hasMedia: event.hasMedia,
        mediaType: event.mediaType,
        mediaUrls: event.mediaUrls,
      });

      // ── Interceptor determinístico de BAJA ───────────────────────────
      // Antes de encolar al LLM: una baja inequívoca no consume un token ni
      // pasa por Enua. Se responde el texto fijo y se persiste el opt-out por
      // HTTP (best-effort). Grupos y mensajes del dueño quedan fuera: ahí no
      // hay un paciente que pueda darse de baja.
      if (!isGroup && !msg.key.fromMe && !event.fromOwner && esOptOutDeterministico(event.body)) {
        // Solo un teléfono real sirve para persistir el opt-out: si el chat
        // llegó por LID y no hay mapeo, no se inventa un número.
        const telefonoOptOut = senderPhone
          || (String(chatId || '').endsWith('@s.whatsapp.net')
            ? String(chatId).replace(/@.*/, '').replace(/\D/g, '')
            : '');
        console.log(`[bridge] optout_interceptado telefono=${maskOptOutPhone(telefonoOptOut)}`);
        notificarOptOutBackend({
          telefono: telefonoOptOut,
          texto: event.body,
          messageId: msg.key.id,
        });
        try {
          await sendWithTimeout(chatId, { text: OPTOUT_RESPUESTA_FIJA });
        } catch (err) {
          console.warn('[bridge] optout_respuesta_fija_error:', err.message);
        }
        emitDebugEvent({
          stage: 'optout_interceptado',
          chatId: redactWhatsAppId(chatId),
          messageId: msg.key.id,
        });
        continue;
      }

      messageQueue.push(event);
      emitDebugEvent({
        stage: 'queued',
        chatId: redactWhatsAppId(chatId),
        senderId: redactWhatsAppId(senderId),
        fromOwner: !!fromOwner,
        bodyLength: event.body.length,
        hasMedia: event.hasMedia,
        mediaType: event.mediaType,
        queueLength: messageQueue.length,
      });
      if (messageQueue.length > MAX_QUEUE_SIZE) {
        messageQueue.shift();
      }
    }
  });
}

// HTTP server
const app = express();

// Host-header validation — defends against DNS rebinding.
// The bridge binds loopback-only (127.0.0.1) but a victim browser on
// the same machine could be tricked into fetching from an attacker
// hostname that TTL-flips to 127.0.0.1. Reject any request whose Host
// header doesn't resolve to a loopback alias.
// See GHSA-ppp5-vxwm-4cf7.
const _ACCEPTED_HOST_VALUES = new Set([
  'localhost',
  '127.0.0.1',
  '[::1]',
  '::1',
]);

app.use((req, res, next) => {
  const raw = (req.headers.host || '').trim();
  if (!raw) {
    return res.status(400).json({ error: 'Missing Host header' });
  }
  // Strip port suffix: "localhost:3000" → "localhost"
  const hostOnly = (raw.includes(':')
    ? raw.substring(0, raw.lastIndexOf(':'))
    : raw
  ).replace(/^\[|\]$/g, '').toLowerCase();
  if (!_ACCEPTED_HOST_VALUES.has(hostOnly)) {
    return res.status(400).json({
      error: 'Invalid Host header. Bridge accepts loopback hosts only.',
    });
  }
  next();
});

// Challenge-response identity proof: the adapter sends a fresh nonce and no credential, and
// adopts this listener only when the returned HMAC shows it already holds the session secret.
// Registered BEFORE the bearer gate so the adapter never discloses the secret to an impostor
// that bound the port first (it could otherwise replay a reflected bearer as its own proof).
app.get('/auth-proof', (req, res) => {
  const challenge = req.headers['x-hermes-bridge-challenge'];
  if (typeof challenge !== 'string' || !challenge) {
    return res.status(400).json({ error: 'Missing bridge challenge' });
  }
  res.json({ authProof: bridgeAuthProof(BRIDGE_TOKEN, challenge) });
});

// Every other route (including /health and /messages — inbound text is as sensitive as outbound)
// requires `Authorization: Bearer <HERMES_WHATSAPP_BRIDGE_TOKEN>`. JSON bodies are parsed only
// after the gate so an unauthenticated caller cannot drive the body parser.
app.use(createBridgeAuthMiddleware(BRIDGE_TOKEN));
app.use(express.json());

// Poll for new messages (long-poll style)
app.get('/messages', (req, res) => {
  const msgs = messageQueue.splice(0, messageQueue.length).concat(deliveryQueue.splice(0, deliveryQueue.length));
  res.json(msgs);
});

// Send a message
app.post('/send', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }

  const { chatId, message, replyTo, mentions } = req.body;
  if (!chatId || !message) {
    return res.status(400).json({ error: 'chatId and message are required' });
  }

  // Handoff humano activo en este chat → no mandamos nada. El humano es
  // dueño de la conversación hasta que venza el TTL.
  if (isUnderOwnerTakeover(chatId)) {
    return res.json({ success: true, skipped: true, reason: 'takeover' });
  }

  // Chat silenciado por un humano (muted_chats.json) → no sale nada.
  const mutedHit = mutedChatsGate.isMuted(chatId, takeoverAliases);
  if (mutedHit) {
    console.warn(`[bridge] envío bloqueado: chat silenciado (${mutedHit.reason || 'sin motivo'})`);
    return res.json({ success: true, skipped: true, reason: 'chat_muted' });
  }

  try {
    const chunks = splitLongMessage(formatOutgoingMessage(message));
    const messageIds = [];
    for (let i = 0; i < chunks.length; i += 1) {
      const { content: payload, options } = buildTextSendPayload(chunks[i], {
        chatId,
        replyTo: i === 0 ? replyTo : undefined,
        mentions: i === 0 ? mentions : undefined,
        messageStore,
      });
      const sent = await sendWithTimeout(chatId, payload, options);
      trackSentMessageId(sent);
      messageStore.remember(sent);
      if (sent?.key?.id) messageIds.push(sent.key.id);
      if (chunks.length > 1 && i < chunks.length - 1) {
        await sleep(CHUNK_DELAY_MS);
      }
    }

    res.json({
      success: true,
      messageId: messageIds[messageIds.length - 1],
      messageIds,
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Edit a previously sent message
app.post('/edit', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }

  const { chatId, messageId, message } = req.body;
  if (!chatId || !messageId || !message) {
    return res.status(400).json({ error: 'chatId, messageId, and message are required' });
  }

  try {
    const key = { id: messageId, fromMe: true, remoteJid: chatId };
    const chunks = splitLongMessage(formatOutgoingMessage(message));
    const messageIds = [];

    await sendWithTimeout(chatId, { text: chunks[0], edit: key });
    if (chunks.length > 1) {
      for (let i = 1; i < chunks.length; i += 1) {
        const sent = await sendWithTimeout(chatId, { text: chunks[i] });
        trackSentMessageId(sent);
        if (sent?.key?.id) messageIds.push(sent.key.id);
        if (i < chunks.length - 1) {
          await sleep(CHUNK_DELAY_MS);
        }
      }
    }

    res.json({ success: true, messageIds });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Send media (image, video, document) natively
app.post('/send-media', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }

  const { chatId, filePath, mediaType, caption, fileName, mentions } = req.body;
  if (!chatId || !filePath) {
    return res.status(400).json({ error: 'chatId and filePath are required' });
  }

  // Handoff humano activo → no mandamos adjuntos tampoco.
  if (isUnderOwnerTakeover(chatId)) {
    return res.json({ success: true, skipped: true, reason: 'takeover' });
  }

  // Chat silenciado por un humano (muted_chats.json) → no sale nada.
  const mutedHit = mutedChatsGate.isMuted(chatId, takeoverAliases);
  if (mutedHit) {
    console.warn(`[bridge] envío bloqueado: chat silenciado (${mutedHit.reason || 'sin motivo'})`);
    return res.json({ success: true, skipped: true, reason: 'chat_muted' });
  }

  try {
    if (!existsSync(filePath)) {
      return res.status(404).json({ error: `File not found: ${filePath}` });
    }

    const buffer = readFileSync(filePath);
    const ext = filePath.toLowerCase().split('.').pop();
    const type = mediaType || inferMediaType(ext);
    let msgPayload;

    switch (type) {
      case 'image':
        if (ext === 'gif') {
          // WhatsApp's native animated-GIF UX is an MP4 video payload with
          // gifPlayback=true. Convert when ffmpeg is available; otherwise fall
          // back to a truthful image/gif send instead of mislabeling GIF bytes
          // as video/mp4.
          let tmpGifMp4 = null;
          try {
            tmpGifMp4 = path.join(tmpdir(), `hermes_gif_${randomBytes(6).toString('hex')}.mp4`);
            execFileSync(
              'ffmpeg',
              ['-y', '-i', filePath, '-movflags', 'faststart', '-pix_fmt', 'yuv420p', '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2', tmpGifMp4],
              { timeout: 30000, stdio: 'pipe' }
            );
            msgPayload = {
              video: readFileSync(tmpGifMp4),
              caption: caption || undefined,
              mimetype: 'video/mp4',
              gifPlayback: true,
            };
          } catch (gifErr) {
            console.warn('[bridge] gif conversion failed, sending as image/gif:', gifErr.message);
            msgPayload = mediaPayloadForFile({ buffer, filePath, mediaType: type, caption, fileName });
          } finally {
            try { if (tmpGifMp4 && existsSync(tmpGifMp4)) unlinkSync(tmpGifMp4); } catch (_) {}
          }
        } else {
          msgPayload = mediaPayloadForFile({ buffer, filePath, mediaType: type, caption, fileName });
        }
        break;
      case 'video':
        msgPayload = mediaPayloadForFile({ buffer, filePath, mediaType: type, caption, fileName });
        break;
      case 'audio': {
        // WhatsApp only renders a native voice bubble (ptt) when the file is ogg/opus.
        // If the caller passes mp3, wav, m4a etc. (e.g. from Edge TTS / NeuTTS),
        // silently convert to ogg/opus via ffmpeg so ptt is always honoured.
        let audioBuffer = buffer;
        let audioExt = ext;
        const needsConversion = !['ogg', 'opus'].includes(ext);
        let tmpPath = null;
        if (needsConversion) {
          tmpPath = path.join(tmpdir(), `hermes_voice_${randomBytes(6).toString('hex')}.ogg`);
          try {
            execFileSync(
              'ffmpeg',
              ['-y', '-i', filePath, '-ar', '48000', '-ac', '1', '-c:a', 'libopus', tmpPath],
              { timeout: 30000, stdio: 'pipe' }
            );
            audioBuffer = readFileSync(tmpPath);
            audioExt = 'ogg';
          } catch (convErr) {
            // ffmpeg not available or conversion failed — fall back to original format
            console.warn('[bridge] ffmpeg conversion failed, sending as file attachment:', convErr.message);
          } finally {
            try { if (tmpPath && existsSync(tmpPath)) unlinkSync(tmpPath); } catch (_) {}
          }
        }
        const audioMime = (audioExt === 'ogg' || audioExt === 'opus') ? 'audio/ogg; codecs=opus' : 'audio/mpeg';
        msgPayload = { audio: audioBuffer, mimetype: audioMime, ptt: audioExt === 'ogg' || audioExt === 'opus' };
        break;
      }
      case 'document':
      default:
        msgPayload = mediaPayloadForFile({ buffer, filePath, mediaType: 'document', caption, fileName });
        break;
    }

    msgPayload = addMentions(msgPayload, mentions);

    const sent = await sendWithTimeout(chatId, msgPayload);
    trackSentMessageId(sent);
    messageStore.remember(sent);
    res.json({ success: true, messageId: sent?.key?.id });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Send poll primitive. Approval UX is intentionally not wired here; gateway
// approvals need text fallback and explicit confirmation semantics above this
// low-level transport helper.
app.post('/send-poll', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }

  const { chatId, question, options, selectableCount } = req.body;
  if (!chatId || !question || !Array.isArray(options)) {
    return res.status(400).json({ error: 'chatId, question, and options are required' });
  }
  if (mutedChatsGate.isMuted(chatId, takeoverAliases)) {
    return res.json({ success: true, skipped: true, reason: 'chat_muted' });
  }

  try {
    const payload = buildPollPayload({ question, options, selectableCount });
    const sent = await sendWithTimeout(chatId, payload);
    trackSentMessageId(sent);
    rememberSentMessage(sent, payload);
    res.json({ success: true, messageId: sent?.key?.id });
  } catch (err) {
    res.status(400).json({ error: err.message });
  }
});

// Send native WhatsApp location pin
app.post('/send-location', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }

  const { chatId, latitude, longitude, name, address } = req.body;
  if (!chatId || latitude === undefined || longitude === undefined) {
    return res.status(400).json({ error: 'chatId, latitude, and longitude are required' });
  }
  if (mutedChatsGate.isMuted(chatId, takeoverAliases)) {
    return res.json({ success: true, skipped: true, reason: 'chat_muted' });
  }

  try {
    const payload = buildLocationPayload({ latitude, longitude, name, address });
    const sent = await sendWithTimeout(chatId, payload);
    trackSentMessageId(sent);
    messageStore.remember(sent);
    res.json({ success: true, messageId: sent?.key?.id });
  } catch (err) {
    res.status(400).json({ error: err.message });
  }
});

// Typing indicator
app.post('/typing', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected' });
  }

  const { chatId } = req.body;
  if (!chatId) return res.status(400).json({ error: 'chatId required' });

  try {
    await sock.sendPresenceUpdate('composing', chatId);
    res.json({ success: true });
  } catch (err) {
    res.json({ success: false });
  }
});

// Mark an inbound message as read only after the Python adapter has accepted
// it through the authoritative DM/group/mention intake policy.
app.post('/read', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected' });
  }

  const receiptKeys = inboundReadReceiptKeys({
    key: req.body?.key,
    enabled: SEND_READ_RECEIPTS,
  });
  if (receiptKeys.length === 0) {
    return res.json({ success: true, marked: false });
  }

  try {
    await sock.readMessages(receiptKeys);
    return res.json({ success: true, marked: true });
  } catch (err) {
    console.warn('[bridge] failed to send read receipt:', err.message);
    return res.status(500).json({ error: 'Failed to send read receipt' });
  }
});

// Chat info
app.get('/chat/:id', async (req, res) => {
  const chatId = req.params.id;
  const isGroup = chatId.endsWith('@g.us');

  if (isGroup && sock) {
    try {
      const metadata = await sock.groupMetadata(chatId);
      return res.json({
        name: metadata.subject,
        isGroup: true,
        participants: metadata.participants.map(p => p.id),
      });
    } catch {
      // Fall through to default
    }
  }

  res.json({
    name: chatId.replace(/@.*/, ''),
    isGroup,
    participants: [],
  });
});

// Health check
app.get('/health', (req, res) => {
  res.json({
    status: connectionState,
    queueLength: messageQueue.length,
    uptime: process.uptime(),
    scriptHash: SCRIPT_HASH,
    sendReadReceipts: SEND_READ_RECEIPTS,
    // path.resolve, not realpath: the adapter compares against os.path.abspath, which keeps symlinks.
    session: path.resolve(SESSION_DIR),
    capabilities: { outboundMentions: true },
  });
});

// Start
if (PAIR_ONLY) {
  // Pair-only mode: just connect, show QR, save creds, exit. No HTTP server.
  if (PAIR_JSON) {
    emitPairEvent({ event: 'started', session: SESSION_DIR });
  } else {
    console.log('📱 WhatsApp pairing mode');
    console.log(`📁 Session: ${SESSION_DIR}`);
    console.log();
  }
  startSocket().catch((err) => {
    emitPairEvent({ event: 'error', error: err?.message || String(err) });
    if (!PAIR_JSON) {
      console.error(err);
    }
    process.exit(1);
  });
} else {
  if (!BRIDGE_TOKEN) {
    // Refusing beats serving: without a secret every local process could read and send as the user.
    console.error('HERMES_WHATSAPP_BRIDGE_TOKEN is required; start the bridge through `hermes gateway`.');
    process.exit(1);
  }
  const server = app.listen(PORT, '127.0.0.1', () => {
    console.log(`🌉 WhatsApp bridge listening on port ${PORT} (mode: ${WHATSAPP_MODE})`);
    console.log(`📁 Session stored in: ${SESSION_DIR}`);
    if (ALLOWED_USERS.size > 0) {
      console.log(`🔒 Allowed users: ${Array.from(ALLOWED_USERS).join(', ')}`);
    } else if (WHATSAPP_MODE === 'self-chat') {
      console.log(`🔒 Self-chat mode — only your own messages to yourself are processed.`);
    } else if (WHATSAPP_MODE === 'bot' && WHATSAPP_DM_POLICY === 'pairing') {
      console.log(`🤝 WHATSAPP_DM_POLICY=pairing — unknown DMs are forwarded for gateway pairing.`);
    } else {
      console.log(`🔒 No WHATSAPP_ALLOWED_USERS set — incoming messages are rejected.`);
      console.log(`   Set WHATSAPP_ALLOWED_USERS=<phone> to authorize specific users,`);
      console.log(`   or WHATSAPP_ALLOWED_USERS=* for an explicit open bot.`);
    }
    if (WHATSAPP_MODE === 'bot' && FORWARD_OWNER_MESSAGES) {
      console.log(`👤 WHATSAPP_FORWARD_OWNER_MESSAGES=true — owner-typed messages will be forwarded with fromOwner:true`);
    }
    console.log();
    scheduleReconnect(0);
  });
  // Without this handler an EADDRINUSE (an old bridge that has not released the port yet) crashes
  // with a bare stack trace; the adapter's reconnect loop retried blind. Fail loudly and fast.
  server.on('error', (err) => {
    if (err && err.code === 'EADDRINUSE') {
      console.error(`FATAL: port ${PORT} already in use on 127.0.0.1 — another bridge or process is listening there.`);
    } else {
      console.error(`FATAL: bridge HTTP server error: ${err && err.message ? err.message : err}`);
    }
    process.exit(1);
  });
}
