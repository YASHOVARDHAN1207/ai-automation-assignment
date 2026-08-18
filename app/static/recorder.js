/*
 * Browser recorder that produces 16-bit PCM WAV.
 *
 * Why not MediaRecorder? Because it hands back WebM/Opus, and Opus cannot be
 * decoded in pure Python. Every loudness number the server reports would then
 * depend on ffmpeg being installed next to it. Capturing raw Float32 frames
 * from a Web Audio node and writing the 44-byte WAV header here means the
 * server can measure the waveform with the standard library alone.
 *
 * Cost of that choice, stated honestly: WAV is ~10x the size of Opus. That is
 * fine for a 10-second clip and is exactly the thing that breaks first at
 * 5,000 workers - see docs/SCALE_5000.md.
 */
(function () {
  "use strict";

  var form = document.getElementById("submit-form");
  var btnRecord = document.getElementById("btn-record");
  var btnStop = document.getElementById("btn-stop");
  var btnSubmit = document.getElementById("btn-submit");
  var status = document.getElementById("rec-status");
  var timer = document.getElementById("rec-timer");
  var meter = document.getElementById("level-meter");
  var preview = document.getElementById("preview");
  var chosen = document.getElementById("chosen");
  var fileInput = document.getElementById("file");
  var result = document.getElementById("result");

  var audioContext = null;
  var mediaStream = null;
  var processor = null;
  var sourceNode = null;
  var chunks = [];
  var recordedRate = 44100;
  var startedAt = 0;
  var tick = null;

  var payload = null;        // {blob, filename, mode}

  function setStatus(text, className) {
    status.textContent = text;
    status.className = className || "muted";
  }

  function ready(blob, filename, mode) {
    payload = { blob: blob, filename: filename, mode: mode };
    preview.src = URL.createObjectURL(blob);
    preview.hidden = false;
    chosen.textContent = filename + " · " + (blob.size / 1024).toFixed(0) + " KB";
    btnSubmit.disabled = false;
  }

  /* --- WAV encoding ------------------------------------------------------ */

  function encodeWav(buffers, length, sampleRate) {
    var bytes = new ArrayBuffer(44 + length * 2);
    var view = new DataView(bytes);

    function writeString(offset, text) {
      for (var i = 0; i < text.length; i++) {
        view.setUint8(offset + i, text.charCodeAt(i));
      }
    }

    writeString(0, "RIFF");
    view.setUint32(4, 36 + length * 2, true);
    writeString(8, "WAVEfmt ");
    view.setUint32(16, 16, true);          // fmt chunk size
    view.setUint16(20, 1, true);           // PCM
    view.setUint16(22, 1, true);           // mono
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);  // byte rate
    view.setUint16(32, 2, true);           // block align
    view.setUint16(34, 16, true);          // bits per sample
    writeString(36, "data");
    view.setUint32(40, length * 2, true);

    var offset = 44;
    for (var b = 0; b < buffers.length; b++) {
      var chunk = buffers[b];
      for (var i = 0; i < chunk.length; i++) {
        // Clamp before scaling: a value just over 1.0 would wrap to negative.
        var sample = Math.max(-1, Math.min(1, chunk[i]));
        view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
        offset += 2;
      }
    }
    return new Blob([view], { type: "audio/wav" });
  }

  /* --- recording -------------------------------------------------------- */

  function startTimer() {
    startedAt = Date.now();
    tick = setInterval(function () {
      timer.textContent = ((Date.now() - startedAt) / 1000).toFixed(1) + "s";
    }, 100);
  }

  function stopTimer() {
    if (tick) { clearInterval(tick); tick = null; }
  }

  btnRecord.addEventListener("click", function () {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setStatus("this browser cannot record - use the file upload instead", "bad");
      return;
    }
    navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: false }
    }).then(function (stream) {
      mediaStream = stream;
      var Ctx = window.AudioContext || window.webkitAudioContext;
      audioContext = new Ctx();
      recordedRate = audioContext.sampleRate;
      sourceNode = audioContext.createMediaStreamSource(stream);
      processor = audioContext.createScriptProcessor(4096, 1, 1);
      chunks = [];

      processor.onaudioprocess = function (event) {
        var input = event.inputBuffer.getChannelData(0);
        chunks.push(new Float32Array(input));
        var peak = 0;
        for (var i = 0; i < input.length; i += 16) {
          var value = Math.abs(input[i]);
          if (value > peak) { peak = value; }
        }
        meter.style.width = Math.min(100, peak * 140).toFixed(0) + "%";
      };

      sourceNode.connect(processor);
      // ScriptProcessor only fires while connected to a destination. A zero-gain
      // node keeps it running without playing the mic back through the speakers.
      var mute = audioContext.createGain();
      mute.gain.value = 0;
      processor.connect(mute);
      mute.connect(audioContext.destination);

      btnRecord.disabled = true;
      btnStop.disabled = false;
      setStatus("recording at " + (recordedRate / 1000).toFixed(1) + " kHz", "rec");
      startTimer();
    }).catch(function (error) {
      setStatus("microphone denied: " + error.message + " - use the file upload", "bad");
    });
  });

  btnStop.addEventListener("click", function () {
    stopTimer();
    meter.style.width = "0%";
    if (processor) { processor.onaudioprocess = null; processor.disconnect(); }
    if (sourceNode) { sourceNode.disconnect(); }
    if (mediaStream) { mediaStream.getTracks().forEach(function (t) { t.stop(); }); }
    if (audioContext) { audioContext.close(); }

    var total = chunks.reduce(function (sum, chunk) { return sum + chunk.length; }, 0);
    if (!total) {
      setStatus("nothing was captured", "bad");
      btnRecord.disabled = false;
      btnStop.disabled = true;
      return;
    }
    var blob = encodeWav(chunks, total, recordedRate);
    ready(blob, "recording-" + Date.now() + ".wav", "browser_recording");
    setStatus("recorded " + (total / recordedRate).toFixed(1) + "s", "ok");
    btnRecord.disabled = false;
    btnStop.disabled = true;
  });

  fileInput.addEventListener("change", function () {
    var file = fileInput.files && fileInput.files[0];
    if (file) {
      ready(file, file.name, "file_upload");
      setStatus("file selected", "ok");
    }
  });

  /* --- submit ----------------------------------------------------------- */

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (!payload) { return; }

    var body = new FormData();
    body.append("name", document.getElementById("name").value);
    body.append("phone", document.getElementById("phone").value);
    body.append("capture_mode", payload.mode);
    body.append("audio", payload.blob, payload.filename);

    btnSubmit.disabled = true;
    btnSubmit.textContent = "Analysing…";
    result.innerHTML = "";

    fetch("/api/submissions", { method: "POST", body: body })
      .then(function (response) { return response.json().then(function (data) {
        return { ok: response.ok, data: data };
      }); })
      .then(function (outcome) {
        btnSubmit.textContent = "Submit";
        if (!outcome.ok || !outcome.data.ok) {
          result.innerHTML = '<div class="card bad">' +
            escapeHtml(outcome.data.error || "submission failed") + "</div>";
          btnSubmit.disabled = false;
          return;
        }
        result.innerHTML = renderResult(outcome.data);
      })
      .catch(function (error) {
        btnSubmit.textContent = "Submit";
        btnSubmit.disabled = false;
        result.innerHTML = '<div class="card bad">' + escapeHtml(error.message) + "</div>";
      });
  });

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function row(label, value) {
    if (value === null || value === undefined || value === "") { return ""; }
    return "<div><dt>" + escapeHtml(label) + "</dt><dd>" + escapeHtml(value) + "</dd></div>";
  }

  function renderResult(data) {
    var s = data.submission;
    var link = {
      matched_existing: "matched an existing person in the database",
      created_contact: "new contact - added as a fourth source, will survive the next rebuild",
      unlinked: "phone could not be parsed, so no identity was claimed"
    }[data.person_link_method] || data.person_link_method;

    var html = '<div class="card ok">' +
      "<h3>Stored as submission #" + s.submission_id + "</h3>" +
      "<p>" + escapeHtml(link) +
      (data.person_id ? " (person #" + data.person_id + ")" : "") + "</p>";

    if (data.duplicate_of) {
      html += '<p class="warn">Identical audio was already submitted as #' +
        data.duplicate_of + " (matched by sha256).</p>";
    }

    if (s.analysis_ok) {
      html += "<dl>" +
        row("Duration", s.duration_seconds + " s") +
        row("Sample rate", s.sample_rate_khz + " kHz") +
        row("Bitrate", s.bitrate_kbps + " kbps") +
        row("Channels", s.channels) +
        row("Bit depth", s.bit_depth ? s.bit_depth + "-bit" : null) +
        row("Peak", s.peak_dbfs + " dBFS") +
        row("RMS", s.rms_dbfs + " dBFS") +
        row("Loudness", s.loudness_lufs !== null ? s.loudness_lufs + " LUFS" : null) +
        row("Noise floor", s.noise_floor_dbfs + " dBFS") +
        row("SNR", s.estimated_snr_db !== null ? s.estimated_snr_db + " dB" : "not estimated") +
        row("Clipping", s.clipping_pct + " %") +
        row("Silence", s.silence_pct + " %") +
        row("Quality", s.quality_score + "/100 (" + s.quality_label + ")") +
        "</dl>";
      if (s.quality_reasons && s.quality_reasons.length) {
        html += "<ul>";
        s.quality_reasons.forEach(function (reason) {
          html += "<li>" + escapeHtml(reason) + "</li>";
        });
        html += "</ul>";
      }
    } else {
      html += '<p class="warn">Stored, but the waveform could not be analysed: ' +
        escapeHtml(s.analysis_note || "unknown reason") + "</p>";
    }

    html += '<p><a href="/submissions">See all submissions &rarr;</a></p></div>';
    return html;
  }
}());
