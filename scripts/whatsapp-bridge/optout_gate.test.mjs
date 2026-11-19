import test from 'node:test';
import assert from 'node:assert/strict';

import {
  normalizarParaBaja,
  esOptOutDeterministico,
} from './optout_gate.js';

test('normalizarParaBaja: minúsculas, sin tildes, sin signos ni espacios múltiples', () => {
  assert.equal(normalizarParaBaja('  ¡BAJA! '), 'baja');
  assert.equal(normalizarParaBaja('No me escriban MÁS, por favor...'), 'no me escriban mas por favor');
  assert.equal(normalizarParaBaja(''), '');
  assert.equal(normalizarParaBaja(null), '');
  assert.equal(normalizarParaBaja(undefined), '');
});

test('esOptOutDeterministico: casos positivos (variantes exactas y prefijos cortos)', () => {
  const positivos = [
    'BAJA!',
    'baja',
    '  Baja. ',
    '¡BAJA POR FAVOR!',
    'STOP',
    'stop!',
    'unsubscribe',
    'no gracias',
    'no quiero',
    'no quiero mas',
    'no quiero más',
    'no quiero recibir mas',
    'no quiero recibir más',
    'no quiero recibir mas mensajes',
    'no quiero recibir más mensajes de ustedes',
    'no me escriban',
    'no me escriban más',
    'no me escriban mas por favor',
    'no me manden mas',
    'no me manden más mensajes',
    'no me envien mas',
    'no me contacten',
    'no me contacten más',
    'dar de baja',
    'darme de baja',
    'darme de baja de la lista',
    'quiero darme de baja',
    'quiero darme de baja por favor',
    'solicito la baja',
    'baja suscripcion',
    'baja newsletter',
    'baja de difusion',
    'eliminar mi numero',
    'borrar mi numero',
  ];

  for (const msg of positivos) {
    assert.equal(
      esOptOutDeterministico(msg),
      true,
      `Debería detectar opt-out positivo para: "${msg}"`
    );
  }
});

test('esOptOutDeterministico: casos negativos (turnos, consultas clínicas, mensajes largos o vacíos)', () => {
  const negativos = [
    'quiero dar de baja un turno',
    'necesito dar de baja el turno de mañana',
    'hola quiero dar de baja mi turno con la doctora',
    'cancelar mi turno',
    'baja de turno',
    'tengo baja presión y quería consultar si puedo tomar el antibiótico',
    'me duele la muela de abajo',
    'baja un poco el dolor cuando tomo frío',
    'hola buen día, ¿tienen turno para limpieza esta semana?',
    'no quiero el turno del martes, prefiero el jueves a la tarde',
    'sí, me interesa agendar un control',
    // Más de 12 palabras aunque empiece con prefijo de baja: debe ir al flujo normal / no disparar opt-out ciego
    'quiero darme de baja pero antes necesito que me envíen la radiografía que me hicieron el mes pasado en el consultorio',
    '',
    '   ',
    null,
    undefined,
  ];

  for (const msg of negativos) {
    assert.equal(
      esOptOutDeterministico(msg),
      false,
      `NO debería clasificar como opt-out: "${String(msg)}"`
    );
  }
});
