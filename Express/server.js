const express = require("express");
const fs = require("fs");
const fsp = require("fs").promises;
const os = require("os");
const path = require("path");
const sharp = require("sharp");
const { spawn } = require("child_process");

// Root image dir and special subfolders
const OUT_DIR = "C:/Users/noman_j/Desktop/Finger/images";
const TEST_DIR = path.join(OUT_DIR, "test"); // CLS probe images
const INBOX_DIR = path.join(OUT_DIR, "inbox"); // fallback if no PID provided

// We ONLY save .png
const OUTPUT_EXT = "png";

// AFISNet integration (EDIT these paths if needed)
const AFIS = {
  EXE: "C:/Users/noman_j/Desktop/Finger/AfisNet/bin/Release/net8.0/AfisNet.exe",
  DB: "C:/Users/noman_j/Desktop/Finger/AfisNet/db",
  DPI: "500",
  THRESHOLD: "45", // will be overridden by threshold.json if present
  STAGING: "C:/Users/noman_j/Desktop/Finger/AfisNet/_enroll_staging",
};
const THRESH_FILE = path.join(AFIS.DB, "threshold.json");

// Ensure base folders exist
for (const p of [OUT_DIR, TEST_DIR, INBOX_DIR, AFIS.DB]) {
  if (!fs.existsSync(p)) fs.mkdirSync(p, { recursive: true });
}

// Load threshold if saved previously
(function loadThresholdFromDisk() {
  try {
    if (fs.existsSync(THRESH_FILE)) {
      const obj = JSON.parse(fs.readFileSync(THRESH_FILE, "utf8"));
      if (obj && obj.threshold != null) {
        AFIS.THRESHOLD = String(obj.threshold);
        console.log("[AFIS] Loaded threshold from disk:", AFIS.THRESHOLD);
      }
    }
  } catch (e) {
    console.warn("[AFIS] Failed to load threshold.json:", e.message);
  }
})();
async function saveThresholdToDisk(thresholdNumber) {
  try {
    await fsp.mkdir(AFIS.DB, { recursive: true });
    await fsp.writeFile(
      THRESH_FILE,
      JSON.stringify({ threshold: Number(thresholdNumber) }, null, 2)
    );
    console.log("[AFIS] Saved threshold to disk:", thresholdNumber);
  } catch (e) {
    console.warn("[AFIS] Failed to save threshold.json:", e.message);
  }
}

// Unpack packed4 format to raw 8-bit grayscale
function unpackPacked4ToRaw8(buf, w, h) {
  const out = Buffer.alloc(w * h);
  let j = 0;
  for (let i = 0; i < buf.length && j < out.length; i++) {
    const b = buf[i];
    out[j++] = ((b >> 4) & 0x0f) * 17; // 0..15 -> 0..255
    if (j < out.length) out[j++] = (b & 0x0f) * 17;
  }
  return out;
}

// Minimal PGM P5 parser (accept PGM input; we still SAVE PNG only)
function parsePGMtoRaw8(buf) {
  let i = 0;
  const isWS = (b) => b === 9 || b === 10 || b === 13 || b === 32;
  const eatWS = () => {
    while (i < buf.length && isWS(buf[i])) i++;
  };
  const readTok = () => {
    eatWS();
    if (buf[i] === 0x23) {
      while (i < buf.length && buf[i] !== 10) i++;
      return readTok();
    }
    const s = i;
    while (i < buf.length && !isWS(buf[i])) i++;
    return buf.slice(s, i).toString("ascii");
  };
  if (buf.slice(0, 2).toString("ascii") !== "P5") throw new Error("not PGM P5");
  i += 2;
  const w = parseInt(readTok(), 10);
  const h = parseInt(readTok(), 10);
  const maxval = parseInt(readTok(), 10);
  if (!(w > 0 && h > 0 && maxval > 0)) throw new Error("bad PGM header");
  if (buf[i] === 10 || buf[i] === 13) i++;
  return { raw8: buf.slice(i), w, h };
}

// ---------- AFIS runners (with strong error handling) ----------
function runAfisIdentify(probePath) {
  return new Promise((resolve, reject) => {
    try {
      if (!fs.existsSync(AFIS.EXE))
        return reject(new Error(`AfisNet.exe not found: ${AFIS.EXE}`));
      if (!fs.existsSync(probePath))
        return reject(new Error(`probe image not found: ${probePath}`));
      if (!fs.existsSync(AFIS.DB))
        return reject(new Error(`AFIS DB folder not found: ${AFIS.DB}`));

      const args = [
        "identify",
        "--probe",
        probePath,
        "--db",
        AFIS.DB,
        "--threshold",
        AFIS.THRESHOLD,
        "--dpi",
        AFIS.DPI,
      ];
      console.log("[AFIS spawn identify]", { exe: AFIS.EXE, args });

      const p = spawn(AFIS.EXE, args, {
        windowsHide: true,
        cwd: path.dirname(AFIS.EXE),
      });
      let out = "",
        err = "";
      p.on("error", (e) => reject(new Error(`spawn failed: ${e.message}`)));
      p.stdout.on("data", (d) => (out += d.toString()));
      p.stderr.on("data", (d) => (err += d.toString()));
      p.on("close", (code) => {
        if (code !== 0) return reject(new Error(err || `AfisNet exit ${code}`));
        try {
          const obj = JSON.parse(out); // { match_id | matchId, score, threshold }
          console.log("[AFIS result]", obj);
          resolve(obj);
        } catch {
          reject(new Error(`AfisNet bad JSON. stdout=${out} stderr=${err}`));
        }
      });
    } catch (e) {
      reject(e);
    }
  });
}

/**
 * Build a staging gallery from OUT_DIR, but copy **only numeric person folders**:
 *   OUT_DIR/<pid>/*.png  -->  STAGING/<pid>/*.png
 * Skips OUT_DIR/test and OUT_DIR/inbox automatically.
 */
async function prepareNumericOnlyStaging() {
  try {
    await fsp.rm(AFIS.STAGING, { recursive: true, force: true });
  } catch {}
  await fsp.mkdir(AFIS.STAGING, { recursive: true });

  const entries = await fsp.readdir(OUT_DIR, { withFileTypes: true });
  const numericDirs = entries.filter(
    (e) => e.isDirectory() && /^\d+$/.test(e.name)
  );
  for (const ent of numericDirs) {
    const srcDir = path.join(OUT_DIR, ent.name);
    const dstDir = path.join(AFIS.STAGING, ent.name);
    await fsp.mkdir(dstDir, { recursive: true });

    const files = await fsp.readdir(srcDir, { withFileTypes: true });
    for (const f of files) {
      if (!f.isFile()) continue;
      const ext = path.extname(f.name).toLowerCase();
      if (ext !== ".png") continue; // PNG-only
      await fsp.copyFile(path.join(srcDir, f.name), path.join(dstDir, f.name));
    }
  }
  return AFIS.STAGING;
}

function runAfisEnroll(galleryRoot) {
  return new Promise((resolve, reject) => {
    if (!fs.existsSync(AFIS.EXE))
      return reject(new Error(`AfisNet.exe not found: ${AFIS.EXE}`));
    const args = [
      "enroll",
      "--gallery",
      galleryRoot,
      "--db",
      AFIS.DB,
      "--dpi",
      AFIS.DPI,
    ];
    console.log("[AFIS spawn enroll]", { exe: AFIS.EXE, args });

    const p = spawn(AFIS.EXE, args, {
      windowsHide: true,
      cwd: path.dirname(AFIS.EXE),
    });
    let out = "",
      err = "";
    p.on("error", (e) => reject(new Error(`spawn failed: ${e.message}`)));
    p.stdout.on("data", (d) => (out += d.toString()));
    p.stderr.on("data", (d) => (err += d.toString()));
    p.on("close", (code) =>
      code === 0
        ? resolve(out)
        : reject(new Error(err || `AfisNet enroll exit ${code}`))
    );
  });
}

function runAfisCalibrate(galleryRoot, far = "0.001") {
  return new Promise((resolve, reject) => {
    if (!fs.existsSync(AFIS.EXE))
      return reject(new Error(`AfisNet.exe not found: ${AFIS.EXE}`));
    const args = [
      "calibrate",
      "--gallery",
      galleryRoot,
      "--db",
      AFIS.DB,
      "--far",
      far,
      "--dpi",
      AFIS.DPI,
    ];
    console.log("[AFIS spawn calibrate]", { exe: AFIS.EXE, args });

    const p = spawn(AFIS.EXE, args, {
      windowsHide: true,
      cwd: path.dirname(AFIS.EXE),
    });
    let out = "",
      err = "";
    p.on("error", (e) => reject(new Error(`spawn failed: ${e.message}`)));
    p.stdout.on("data", (d) => (out += d.toString()));
    p.stderr.on("data", (d) => (err += d.toString()));
    p.on("close", (code) => {
      if (code !== 0)
        return reject(new Error(err || `AfisNet calibrate exit ${code}`));
      try {
        resolve(JSON.parse(out));
      } catch {
        reject(new Error(`AfisNet bad JSON. stdout=${out} stderr=${err}`));
      }
    });
  });
}

async function clearAfisDb() {
  await fsp.rm(AFIS.DB, { recursive: true, force: true });
  await fsp.mkdir(AFIS.DB, { recursive: true });
  console.log("[AFIS] Cleared DB:", AFIS.DB);
}

// ================== APP ==================
const app = express();
const PORT = 3000;

app.use(express.json({ limit: "20mb" }));

app.get("/", (_req, res) => {
  res.send(
    "POST packed4/raw8/pgm to /upload-image. Enroll images saved under /images/<pid>/<pid>_#.png; CLS probes saved in /images/test. Add X-Person-Id for enroll; X-Mode: cls for test; X-Identify: 1 to auto-identify."
  );
});

// Upload route: accepts raw bytes; writes PNG only; optional AFIS identify
app.post(
  "/upload-image",
  express.raw({ type: "application/octet-stream", limit: "10mb" }),
  async (req, res) => {
    try {
      if (!req.body || !Buffer.isBuffer(req.body) || req.body.length === 0) {
        return res.status(400).send("no image received");
      }

      // Hints from sender (defaults for R307)
      const fmt = (req.headers["x-format"] || "packed4")
        .toString()
        .toLowerCase(); // packed4 | raw8 | pgm
      const W = parseInt(req.headers["x-width"] || "256", 10);
      const H = parseInt(req.headers["x-height"] || "288", 10);
      let baseName = (req.headers["x-filename"] || `img_${Date.now()}`)
        .toString()
        .replace(/\.[^/.]+$/, "");
      const autoIdentify =
        (req.headers["x-identify"] || "1").toString() === "1";
      const modeHeader = (req.headers["x-mode"] || "").toString().toLowerCase();
      const pidHeader = (req.headers["x-person-id"] || "").toString().trim();

      // Decide folder:
      //  - CLS -> images/test
      //  - Enroll with numeric PID -> images/<pid>
      //  - Fallback -> images/inbox
      let saveDir;
      if (modeHeader === "cls") {
        saveDir = TEST_DIR;
      } else if (/^\d+$/.test(pidHeader)) {
        saveDir = path.join(OUT_DIR, pidHeader);
        if (!fs.existsSync(saveDir)) fs.mkdirSync(saveDir, { recursive: true });
        // Ensure filename starts with "<pid>_"
        if (!new RegExp(`^${pidHeader}_`).test(baseName)) {
          baseName = `${pidHeader}_${baseName}`;
        }
      } else {
        saveDir = INBOX_DIR;
      }

      // Decode to raw 8-bit
      let raw8,
        w = W,
        h = H;
      if (fmt === "packed4") {
        raw8 = unpackPacked4ToRaw8(req.body, W, H);
      } else if (fmt === "raw8") {
        if (req.body.length !== W * H)
          return res.status(400).send("raw8 length mismatch");
        raw8 = req.body;
      } else if (fmt === "pgm") {
        const p = parsePGMtoRaw8(req.body);
        raw8 = p.raw8;
        w = p.w;
        h = p.h;
      } else {
        return res.status(400).send("unsupported format");
      }

      // Write PNG only
      const outPath = path.join(saveDir, `${baseName}.${OUTPUT_EXT}`);
      const img = sharp(raw8, { raw: { width: w, height: h, channels: 1 } });
      await img.png().toFile(outPath);
      console.log(`[SAVE] ${outPath}`);

      if (!autoIdentify) {
        return res.type("text/plain").send("next");
      }

      // Identify using AFIS
      try {
        const result = await runAfisIdentify(outPath);
        const matchId =
          result.match_id !== undefined && result.match_id !== null
            ? result.match_id
            : result.matchId !== undefined
            ? result.matchId
            : null;

        return res.json({
          ok: true,
          file: outPath,
          match_id: matchId,
          score: result.score,
          threshold: result.threshold,
        });
      } catch (e) {
        console.error("AFIS identify failed:", e.message);
        return res.status(500).json({
          ok: false,
          error: "afis_failed",
          detail: e.message,
          file: outPath,
        });
      }
    } catch (e) {
      console.error("Upload error:", e);
      return res.status(500).send("error");
    }
  }
);

// -------- DB Maintenance APIs (numeric-only staging) --------

// Re-enroll templates from numeric PID folders (uses staging to exclude test/inbox)
app.post("/afis/enroll", async (_req, res) => {
  try {
    const staging = await prepareNumericOnlyStaging();
    const out = await runAfisEnroll(staging);
    try {
      await fsp.rm(staging, { recursive: true, force: true });
    } catch {}
    res.json({ ok: true, out });
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message });
  }
});

// Calibrate threshold and persist to threshold.json (numeric-only staging)
app.post("/afis/calibrate", async (req, res) => {
  try {
    const far = (req.query.far || "0.001").toString();
    const staging = await prepareNumericOnlyStaging();
    const obj = await runAfisCalibrate(staging, far);
    try {
      await fsp.rm(staging, { recursive: true, force: true });
    } catch {}

    if (obj && obj.suggested_threshold != null) {
      AFIS.THRESHOLD = String(obj.suggested_threshold);
      await saveThresholdToDisk(obj.suggested_threshold);
    }
    res.json({ ok: true, ...obj, threshold_in_use: AFIS.THRESHOLD });
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message });
  }
});

// Clear DB folder
app.post("/afis/clear-db", async (_req, res) => {
  try {
    await clearAfisDb();
    res.json({ ok: true, cleared: true });
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message });
  }
});

// One-shot: clear DB (optional), enroll, calibrate, persist threshold
app.post("/afis/rebuild", async (req, res) => {
  try {
    const far = (req.query.far || "0.001").toString();
    const clear = (req.query.clear || "1").toString() === "1";

    if (clear) await clearAfisDb();

    const staging = await prepareNumericOnlyStaging();
    const enrollOut = await runAfisEnroll(staging);
    const calObj = await runAfisCalibrate(staging, far);
    try {
      await fsp.rm(staging, { recursive: true, force: true });
    } catch {}

    if (calObj && calObj.suggested_threshold != null) {
      AFIS.THRESHOLD = String(calObj.suggested_threshold);
      await saveThresholdToDisk(calObj.suggested_threshold);
    }

    res.json({
      ok: true,
      cleared: clear,
      enroll_log: enrollOut,
      calibration: calObj,
      threshold_in_use: AFIS.THRESHOLD,
    });
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message });
  }
});

// Identify an arbitrary file already on disk (JSON body: { "path": "C:\\...\\file.png" })
app.post("/afis/identify", async (req, res) => {
  try {
    const pth = req.body && req.body.path ? req.body.path.toString() : "";
    if (!pth)
      return res.status(400).json({ ok: false, error: "path required" });
    const result = await runAfisIdentify(pth);
    const matchId =
      result.match_id !== undefined && result.match_id !== null
        ? result.match_id
        : result.matchId !== undefined
        ? result.matchId
        : null;
    res.json({
      ok: true,
      match_id: matchId,
      score: result.score,
      threshold: result.threshold,
      file: pth,
    });
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message });
  }
});

// Debug endpoint
app.get("/afis/debug", async (_req, res) => {
  let templateCount = 0;
  try {
    if (fs.existsSync(AFIS.DB)) {
      const dirents = await fsp.readdir(AFIS.DB, { withFileTypes: true });
      for (const d of dirents) {
        if (d.isDirectory()) {
          const files = await fsp.readdir(path.join(AFIS.DB, d.name));
          templateCount += files.length;
        }
      }
    }
  } catch {}
  res.json({
    exe: AFIS.EXE,
    exeExists: fs.existsSync(AFIS.EXE),
    db: AFIS.DB,
    dbExists: fs.existsSync(AFIS.DB),
    threshold: AFIS.THRESHOLD,
    thresholdFileExists: fs.existsSync(THRESH_FILE),
    outDir: OUT_DIR,
    testDir: TEST_DIR,
    inboxDir: INBOX_DIR,
    templateCount,
  });
});

// Start + graceful shutdown
const appInstance = app.listen(PORT, "0.0.0.0", () => {
  console.log(`Express listening on port ${PORT}`);
  const nets = os.networkInterfaces();
  for (const name of Object.keys(nets)) {
    for (const net of nets[name]) {
      if (net.family === "IPv4" && !net.internal) {
        console.log(`→  http://${net.address}:${PORT}`);
      }
    }
  }
});

process.on("unhandledRejection", (r) =>
  console.error("UnhandledRejection:", r)
);
process.on("uncaughtException", (e) => console.error("UncaughtException:", e));
process.on("SIGINT", async () => {
  console.log("\nShutting down...");
  try {
    await fsp.rm(AFIS.STAGING, { recursive: true, force: true });
  } catch {}
  appInstance.close(() => process.exit(0));
});
