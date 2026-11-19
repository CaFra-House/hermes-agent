// Pairing code rotativo: reintenta cada ~50 s y mantiene el código vigente en disco.
// Uso: node pair-loop.mjs <session_dir> <numero> <out_file> [minutos]
import { makeWASocket, useMultiFileAuthState, fetchLatestBaileysVersion, DisconnectReason } from '@whiskeysockets/baileys';
import { writeFileSync, rmSync } from 'fs';

const sessionDir = process.argv[2];
const phoneNumber = String(process.argv[3] || '').replace(/\D/g, '');
const outFile = process.argv[4] || '/tmp/wa-pairing.txt';
const minutes = parseFloat(process.argv[5] || '10');
const DEADLINE = Date.now() + minutes * 60_000;

let attempt = 0;

async function cycle() {
  attempt += 1;
  if (Date.now() > DEADLINE) { console.log('DEADLINE'); process.exit(3); }

  const { state, saveCreds } = await useMultiFileAuthState(sessionDir);
  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false,
    browser: ['Hermes', 'Chrome', '1.0.0'],
  });

  let settled = false;

  sock.ev.on('creds.update', saveCreds);

  // Pedimos el código apenas el socket queda operativo (sin esperar el QR).
  const reqTimer = setTimeout(async () => {
    try {
      const code = await sock.requestPairingCode(phoneNumber);
      writeFileSync(outFile, `${code}\n${Date.now()}`);
      console.log(`ATTEMPT ${attempt} CODE=${code}`);
    } catch (e) {
      console.log(`ATTEMPT ${attempt} PAIRING_ERROR=${e.message}`);
    }
  }, 2500);

  sock.ev.on('connection.update', (u) => {
    const { connection, lastDisconnect } = u;
    if (connection === 'open') {
      settled = true;
      clearTimeout(reqTimer);
      writeFileSync(outFile, `CONNECTED\n${Date.now()}`);
      console.log('CONNECTED');
      setTimeout(() => process.exit(0), 1500);
    }
    if (connection === 'close') {
      const code = lastDisconnect?.error?.output?.statusCode;
      if (code === DisconnectReason.loggedOut) {
        settled = true;
        clearTimeout(reqTimer);
        console.log('LOGGED_OUT');
        process.exit(2);
      }
    }
  });

  // Ventana de 50 s para que el humano escriba el código; después se reinicia
  // el ciclo con un código fresco.
  setTimeout(() => {
    if (settled) return;
    try { sock.end(undefined); } catch {}
    setTimeout(() => { if (!settled) cycle(); }, 1500);
  }, 50_000);
}

cycle();
