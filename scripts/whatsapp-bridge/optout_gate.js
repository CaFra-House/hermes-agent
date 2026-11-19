// Interceptor determinístico de BAJA (opt-out) — módulo puro, sin dependencias.
//
// Un pedido de baja de comunicaciones es corto y cerrado ("BAJA", "no me
// escriban más"). Todo lo demás — agenda, consultas clínicas, párrafos — NO es
// opt-out: pasa a Enua para que gestione el turno o la consulta. El orden de
// evaluación importa: primero se descartan falsos positivos y recién después se
// aplica la lista cerrada de patrones positivos.
//
// Spec: WORKSPACE/PROJECTS/ADEN/PM/PLANO-MOTOR-REACTIVACION-2026-11-16.md
// (Parte A §5.2, Parte C §2). La misma función vive en Python en
// src/backend/marketing/reactivacion.py: deben dar el mismo veredicto.

const MAX_PALABRAS = 12;
const MAX_CARACTERES = 80;

// Prefijo que agrega el gateway al body: "[+5491144445555 | Nombre] hola".
const PREFIJO_RE = /^\s*\[\s*\+?\d[\d\s()+.-]*\|[^\]]*\]\s*/;
const ACENTOS_RE = /[\u0300-\u036f]/g;
const SIGNOS_RE = /[!¡?¿.,;:"'()[\]{}\-_*~]/g;

// Agenda y clínica: si aparece cualquiera de estos términos el mensaje NO es
// una baja de comunicaciones aunque empiece con "baja" o "no quiero".
const TERMINOS_EXCLUIDOS_RE = /\b(turno|turnos|cita|citas|reserva|reservas|horario|agenda|reprogramar|cambiar|presion|fiebre|muela|muelas|diente|hueso|encia|dolor|pastilla|medicamento|antibiotico|abajo|bajar|peso|obra social|prepaga|estudio|panoramica|tomografia)\b/;

// Coincidencia exacta del mensaje completo (ya normalizado).
const EXACTOS = new Set([
  'baja',
  'stop',
  'unsubscribe',
  'cancelar',
  'desuscribir',
  'desuscribirme',
  'no molestar',
  'dar de baja',
  'darme de baja',
  'quiero darme de baja',
  'solicito la baja',
  'baja por favor',
  'quiero la baja',
  'alta baja',
  'no gracias',
  'no quiero',
  'no quiero mas',
  'no quiero recibir mas',
  'baja suscripcion',
  'baja newsletter',
  'baja de difusion',
  'eliminar mi numero',
  'borrar mi numero',
]);

// Coincidencia por patrón sobre mensajes cortos (≤ 12 palabras) sin términos
// excluidos. Lista cerrada: nada fuera de acá dispara el opt-out.
const PATRONES = [
  /^baja\b/,
  /\b(darme de baja|dar de baja|dame de baja)\b/,
  /\b(no me escriban|no me manden|no me envien|no me contacten|dejen de escribir|dejen de escribirme|dejen de mandarme|no escribir mas)\b/,
  /\b(no quiero recibir|no deseo recibir|no quiero mas)\s+(mas\s+)?(mensajes|comunicaciones|info|informacion|whatsapp|notificaciones|publicidad|mensajitos)\b/,
  /\b(eliminar|borrar|quitar|sacar)\s+mi\s+numero\b/,
  /^(solicito|pido|quiero)\s+la\s+baja$/,
];

/**
 * Normaliza un texto entrante para clasificarlo como baja.
 * Quita el prefijo "[+549... | Nombre]", pasa a minúsculas, descompone acentos
 * (NFD sin categoría Mn), reemplaza signos por espacio y colapsa espacios.
 * @param {string|null|undefined} raw
 * @returns {string} texto normalizado; '' si raw es null/undefined/vacío
 */
export function normalizarParaBaja(raw) {
  if (raw === null || raw === undefined) return '';
  return String(raw)
    .replace(PREFIJO_RE, '')
    .normalize('NFD')
    .replace(ACENTOS_RE, '')
    .toLowerCase()
    .replace(SIGNOS_RE, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * true solo si el mensaje es una baja de comunicaciones inequívoca.
 * @param {string|null|undefined} raw
 * @returns {boolean}
 */
export function esOptOutDeterministico(raw) {
  const texto = normalizarParaBaja(raw);
  if (!texto) return false;

  // Regla 1 — falsos positivos (se evalúa PRIMERO): un pedido de baja real es
  // corto; un párrafo largo o un tema de agenda/clínica va a recepción.
  const palabras = texto.split(' ').filter(Boolean);
  if (palabras.length > MAX_PALABRAS || texto.length > MAX_CARACTERES) return false;
  if (TERMINOS_EXCLUIDOS_RE.test(texto)) return false;

  // Regla 2 — lista cerrada de patrones positivos.
  if (EXACTOS.has(texto)) return true;
  return PATRONES.some(re => re.test(texto));
}
