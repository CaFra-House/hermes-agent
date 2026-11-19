/**
 * Pure state helpers for owner takeover ("handoff humano").
 *
 * When the owner (or co-admin) types a message in a patient chat from their
 * phone (linked device), the bot must STOP responding in that chat so the
 * human owns the conversation. There is no manual release command: the chat
 * returns to the bot automatically after a rolling TTL of owner silence
 * (default 12h — every new owner-typed message restarts the window).
 *
 * State shape (persisted as JSON in the session dir):
 *   { "<chatJid>": { "until": <epoch_ms>, "since": <epoch_ms>, "name": <string> } }
 *
 * Everything here is pure (no fs, no Baileys) so it can be unit-tested
 * without spinning up a socket — same pattern as owner_message_gate.js.
 */

export const DEFAULT_TTL_HOURS = 12;

export function parseTakeoverConfig(env) {
  const enabled = String(env.WHATSAPP_TAKEOVER || '').toLowerCase() === 'true';
  const ttlHours = Number.parseFloat(env.WHATSAPP_TAKEOVER_TTL_HOURS) || DEFAULT_TTL_HOURS;
  return { enabled, ttlMs: Math.max(1, ttlHours) * 3600_000 };
}

export function isTakeoverActive(state, chatId, now) {
  const entry = state && state[chatId];
  return !!entry && Number(entry.until) > now;
}

/**
 * Does an active takeover cover this OUTBOUND target? The takeover state is
 * keyed by the inbound chat JID (often `…@lid`), while senders target the
 * phone JID (`…@s.whatsapp.net`) — `aliasesOf(jid)` must return every
 * normalized number that identifies the same person (phone↔LID expansion).
 */
export function isTakeoverActiveForTarget(state, targetJid, now, aliasesOf) {
  if (!state || Object.keys(state).length === 0) return false;
  const wanted = typeof aliasesOf === 'function'
    ? new Set([...aliasesOf(targetJid)].map((v) => String(v)))
    : new Set([String(targetJid || '').replace(/@.*/, '').replace(/:.*$/, '')]);
  for (const [chatJid, entry] of Object.entries(state)) {
    if (!(Number(entry && entry.until) > now)) continue;
    const chatNum = String(chatJid).replace(/@.*/, '').replace(/:.*$/, '');
    if (wanted.has(chatNum)) return true;
  }
  return false;
}

/** Mark an owner-typed message in a chat; returns a NEW state (caller persists it). */
export function markTakeover(state, chatId, now, ttlMs, displayName) {
  const prev = state && state[chatId];
  // Rolling TTL: every owner message extends the window; `since` keeps the
  // original takeover timestamp while the window is still open.
  return {
    ...state,
    [chatId]: {
      since: prev && Number(prev.until) > now ? Number(prev.since) : now,
      until: now + ttlMs,
      name: displayName || (prev && prev.name) || '',
    },
  };
}

export function pruneExpiredTakeovers(state, now) {
  const next = {};
  for (const [chatId, entry] of Object.entries(state || {})) {
    if (Number(entry && entry.until) > now) {
      next[chatId] = entry;
    }
  }
  return next;
}
