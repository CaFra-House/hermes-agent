/**
 * Gate de SALIDA por chat silenciado (muted_chats.json) para el bridge de WhatsApp.
 *
 * El gateway de Hermes ya descarta los mensajes ENTRANTES de un chat silenciado
 * (`_chat_muted_by_human` en gateway/platforms/base.py). Este módulo aplica la
 * MISMA política a los mensajes SALIENTES: si el chat está silenciado y la
 * ventana sigue viva, el bridge no manda nada y responde
 * `{ success: true, skipped: true, reason: 'chat_muted' }`.
 *
 * Misma semántica que el gateway:
 *  - el archivo es `<HERMES_HOME>/muted_chats.json` (HERMES_HOME del perfil);
 *  - cada entrada es `{ "<chat id>": { "until": <epoch SEGUNDOS>, "reason": ... } }`;
 *  - mata si el chat matchea por (a) id literal, (b) dígitos antes de la `@`,
 *    (c) cualquier valor de `aliases` — mismo triple match que la entrada;
 *  - entrada vencida o archivo ausente/ilegible => NO silenciado (fail-open,
 *    igual que el gateway: un archivo roto no puede apagar el WhatsApp entero).
 *
 * `now` en milisegundos; `until` se compara con `now / 1000`.
 */
import { existsSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';

const FILENAME = 'muted_chats.json';

function digits(value) {
  const local = String(value || '').trim().split('@', 1)[0];
  return local.replace(/\D/g, '');
}

/** Rutas candidatas, en orden de prioridad. */
export function mutedChatsPaths(sessionDir, env = process.env) {
  const candidates = [];
  if (env && env.HERMES_HOME) candidates.push(path.join(env.HERMES_HOME, FILENAME));
  const s = path.resolve(sessionDir || '.');
  candidates.push(path.resolve(s, '..', '..', FILENAME)); // <perfil>/platforms/whatsapp/session
  candidates.push(path.resolve(s, '..', FILENAME));
  candidates.push(path.resolve(s, FILENAME));
  return [...new Set(candidates)];
}

/** Primer archivo existente de la lista (o null). */
export function findMutedChatsFile(sessionDir, env = process.env) {
  for (const candidate of mutedChatsPaths(sessionDir, env)) {
    try {
      if (existsSync(candidate)) return candidate;
    } catch {
      /* ruta inaccesible: seguimos */
    }
  }
  return null;
}

/**
 * ¿La entrada cubre este target? `aliasesOf(jid)` (opcional) devuelve todos los
 * identificadores del mismo contacto (teléfono <-> LID), igual que el takeover.
 */
export function isChatMutedInState(state, targetJid, now, aliasesOf) {
  if (!state || typeof state !== 'object') return false;
  const keys = Object.keys(state);
  if (keys.length === 0) return false;

  const wanted = new Set();
  const addTarget = (value) => {
    if (!value) return;
    wanted.add(String(value));
    const d = digits(value);
    if (d) wanted.add(d);
  };
  addTarget(targetJid);
  if (typeof aliasesOf === 'function') {
    try {
      for (const alias of aliasesOf(targetJid)) addTarget(alias);
    } catch {
      /* aliases opcionales: no romper el envío por un mapa raro */
    }
  }

  for (const [chatId, entry] of Object.entries(state)) {
    const until = entry && typeof entry === 'object' ? entry.until : entry;
    const untilMs = Number(until) * 1000;
    if (!Number.isFinite(untilMs) || untilMs <= now) continue;
    const entryKeys = [String(chatId)];
    const aliases = entry && typeof entry === 'object' ? entry.aliases : null;
    if (Array.isArray(aliases)) entryKeys.push(...aliases.filter(Boolean).map(String));
    for (const key of entryKeys) {
      if (wanted.has(key)) return { muted: true, reason: entry && entry.reason ? String(entry.reason) : null, until: untilMs };
      const d = digits(key);
      if (d && wanted.has(d)) return { muted: true, reason: entry && entry.reason ? String(entry.reason) : null, until: untilMs };
    }
  }
  return false;
}

/**
 * Gate con cache por mtime. Devuelve `{ file, state(), isMuted(chatId, aliasesOf, now) }`.
 * `isMuted` devuelve `false` o `{ muted: true, reason, until }`.
 */
export function createMutedChatsGate({ sessionDir, env = process.env, logger = console } = {}) {
  let cachedFile = undefined;
  let cachedMtime = null;
  let cachedState = {};

  function load() {
    const file = findMutedChatsFile(sessionDir, env);
    if (!file) {
      cachedFile = null;
      cachedMtime = null;
      cachedState = {};
      return cachedState;
    }
    let mtime;
    try {
      mtime = statSync(file).mtimeMs;
    } catch {
      return cachedState;
    }
    if (cachedFile === file && cachedMtime === mtime) return cachedState;
    try {
      const parsed = JSON.parse(readFileSync(file, 'utf8') || '{}');
      cachedState = parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
    } catch (err) {
      logger.warn(`[bridge] ${FILENAME} ilegible (${file}): ${err.message} — se trata como vacío`);
      cachedState = {};
    }
    cachedFile = file;
    cachedMtime = mtime;
    return cachedState;
  }

  return {
    get file() {
      return findMutedChatsFile(sessionDir, env);
    },
    state: load,
    isMuted(chatId, aliasesOf, now = Date.now()) {
      return isChatMutedInState(load(), chatId, now, aliasesOf);
    },
  };
}
