/* Jev Terminal frontend — fetches /api/terminal, renders panels + chart.
   Refreshes every 5 minutes while the page is open. */
(function () {
  const $ = (id) => document.getElementById(id);
  const fmt = (n, d = 2) => (n === null || n === undefined || Number.isNaN(n) ? "n/a" : Number(n).toFixed(d));
  const pct = (n) => (n === null || n === undefined ? "n/a" : (n >= 0 ? "+" : "") + Number(n).toFixed(2) + "%");

  function drawChart(bars, pdh, pdl, poc) {
    const cv = $("chart");
    const ctx = cv.getContext("2d");
    const W = cv.width, H = cv.height;
    ctx.clearRect(0, 0, W, H);
    if (!bars || bars.length < 2) {
      ctx.fillStyle = "#8b98a9"; ctx.font = "14px sans-serif";
      ctx.fillText("No bars yet — refresh during market hours.", 16, 30);
      return;
    }
    const closes = bars.map((b) => b.c);
    let lo = Math.min(...closes, pdl || Infinity, poc || Infinity);
    let hi = Math.max(...closes, pdh || -Infinity, poc || -Infinity);
    if (!isFinite(lo) || !isFinite(hi)) { lo = Math.min(...closes); hi = Math.max(...closes); }
    const pad = (hi - lo) * 0.08 || 1;
    lo -= pad; hi += pad;
    const X = (i) => (i / (bars.length - 1)) * (W - 40) + 30;
    const Y = (v) => H - 20 - ((v - lo) / (hi - lo)) * (H - 40);
    const line = (v, color, label) => {
      if (v === null || v === undefined) return;
      ctx.strokeStyle = color; ctx.setLineDash([6, 4]); ctx.beginPath();
      ctx.moveTo(0, Y(v)); ctx.lineTo(W, Y(v)); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = color; ctx.font = "11px sans-serif";
      ctx.fillText(label + " " + fmt(v), 6, Y(v) - 4);
    };
    line(pdh, "#2ea043", "PDH"); line(pdl, "#f85149", "PDL"); line(poc, "#d29922", "POC");
    ctx.strokeStyle = "#58a6ff"; ctx.lineWidth = 1.5; ctx.beginPath();
    bars.forEach((b, i) => { const x = X(i), y = Y(b.c); i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y); });
    ctx.stroke();
    ctx.fillStyle = "#8b98a9"; ctx.font = "11px sans-serif";
    ctx.fillText(fmt(lo, 0), 4, H - 6); ctx.fillText(fmt(hi, 0), 4, 12);
  }

  async function load() {
    try {
      const res = await fetch("/api/terminal");
      const data = await res.json();
      const s = data.snapshot || {};
      const chain = data.chainLog || {};
      const plan = (data.plan && data.plan.payload) || null;
      $("status").textContent = (s.marketStatus || "—") + " · " + (s.istTime || data.nowIst || "") + " · " + (s.feed === "fyers" ? "Fyers engine feed" : "snapshot");
      $("spot").textContent = fmt(s.spot);
      $("spot").className = "big " + ((s.change || 0) >= 0 ? "up" : "down");
      $("change").textContent = (s.change >= 0 ? "+" : "") + fmt(s.change) + " (" + pct(s.changePct) + ") vs PDC " + fmt(s.pdc);
      $("asof").textContent = "As of " + (s.asOf || "—") + " · " + (s.tradingDate || "");
      $("vix").textContent = fmt(s.vix && s.vix.price) + " (" + pct(s.vix && s.vix.changePct) + ")";
      $("bank").textContent = fmt(s.bankNifty && s.bankNifty.price) + " (" + pct(s.bankNifty && s.bankNifty.changePct) + ")";
      $("sensex").textContent = fmt(s.sensex && s.sensex.price) + " (" + pct(s.sensex && s.sensex.changePct) + ")";
      $("market").textContent = s.marketStatus || "—";
      $("pdhpdl").textContent = fmt(s.pdh) + " / " + fmt(s.pdl);
      $("pdc").textContent = fmt(s.pdc);
      $("poc").textContent = fmt(s.weekPoc) + " (" + (s.pocSource || "estimate") + ")";
      $("va").textContent = fmt(s.vah) + " / " + fmt(s.val);
      $("walls").textContent = (chain.callWall ? fmt(chain.callWall, 0) : "n/a") + " / " + (chain.putWall ? fmt(chain.putWall, 0) : "n/a");
      $("maxpain").textContent = (chain.maxPain ? fmt(chain.maxPain, 0) : "n/a") + " / " + (chain.pcr !== undefined && chain.pcr !== null ? chain.pcr : "n/a");
      $("chainDate").textContent = chain.logDate || "not logged today";
      $("chartDate").textContent = s.tradingDate || "—";
      drawChart(s.bars, s.pdh, s.pdl, s.weekPoc);
      $("conds").innerHTML = (s.conditions || []).map((c) =>
        '<div class="cond"><span class="pill ' + c.state + '">' + c.state.toUpperCase() + '</span><b>' + c.label + '</b><br><span class="muted">' + c.detail + '</span></div>').join("") || "—";
      $("atm").textContent = fmt(s.atmStrike, 0);
      $("expiry").textContent = s.expiryDate || "—";
      const g = s.greeks || {};
      $("ce").textContent = "₹" + fmt(g.cePremium) + " / " + fmt(g.ceDelta) + " / ₹" + fmt(g.ceTheta) + "/day";
      $("pe").textContent = "₹" + fmt(g.pePremium) + " / " + fmt(g.peDelta) + " / ₹" + fmt(g.peTheta) + "/day";
      $("gv").textContent = fmt(g.gamma, 4) + " / ₹" + fmt(g.vega);
      $("iv").textContent = fmt(s.ivPct) + "%";
      $("scen").innerHTML = (s.scenarios || []).map((r) =>
        "<tr><td>" + (r.move >= 0 ? "+" : "") + r.move + "</td><td>₹" + fmt(r.ce) + "</td><td>₹" + fmt(r.pe) + "</td></tr>").join("");
      const b = s.blast || {};
      $("blast").innerHTML = b.inWindow
        ? '<span class="pill unmet">IN WINDOW</span> Expiry day, ' + b.window + '. Nearest wall ' + fmt(b.nearestWall, 0) + ' — spot ' + fmt(b.spotNearStrikePct) + '% away.'
        : '<span class="pill met">NOT IN WINDOW</span> ' + (b.isExpiryDay ? "Expiry day, outside " + b.window + "." : "Not expiry day.") + ' ' + (b.note || "");
      if (plan) {
        $("planDate").textContent = plan.planDate || "—";
        $("plan").innerHTML = (plan.checklist || []).map((c) => '<div class="check tone-' + c.tone + '">' + c.label + '</div>').join("");
        $("risk").textContent = (plan.riskRules || []).join(" · ");
        $("planNote").value = (data.plan && data.plan.note) || "";
        $("plan").dataset.planDate = plan.planDate;
      } else {
        $("plan").textContent = "No plan yet — use Rebuild plan once today's snapshot is in.";
      }
      $("events").innerHTML = (data.events || []).map((e) =>
        '<div class="cond"><b>' + e.kind + '</b> <span class="muted">' + (e.at || "") + '</span><br>' + e.text + '</div>').join("") || "No entries yet.";
    } catch (err) {
      $("status").textContent = "Could not load terminal: " + err.message;
    }
  }

  async function post(path, body) {
    const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    return res.json();
  }

  $("chainForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.target);
    const body = {};
    for (const [k, v] of fd.entries()) if (v !== "" && v !== null) body[k] = Number(v);
    const out = await post("/api/chain", body);
    $("chainMsg").textContent = out.ok ? "Logged." : ("Error: " + (out.error || "failed"));
    if (out.ok) load();
  });
  $("eventForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.target);
    await post("/api/events", { kind: fd.get("kind"), text: fd.get("text") });
    ev.target.reset(); load();
  });
  $("rebuildPlan").addEventListener("click", async () => {
    await post("/api/plan/build", {});
    load();
  });
  $("saveNote").addEventListener("click", async () => {
    await post("/api/plan/note", { planDate: $("plan").dataset.planDate, note: $("planNote").value });
  });

  load();
  setInterval(load, 5 * 60 * 1000);
})();
