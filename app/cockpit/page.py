"""The Progress Cockpit page — design taken from the delivered prototype
(docs/prototype-progress-cockpit.html), rendered from live backend data."""

COCKPIT_HTML = """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JARVIS · Progress Cockpit</title>
<style>
  :root{
    --plane:#0a0c10;--surface:#12151c;--surface-2:#171b24;
    --line:rgba(255,255,255,0.08);--ink:#f3f6fa;--ink-2:#9aa4b2;--ink-3:#5c6675;
    --accent:#38bdf8;--accent-2:#3987e5;--good:#22c55e;
    --warn:#fab219;--calm:#2dd4bf;--serious:#ec835a;
    --radius:16px;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;
  }
  *{box-sizing:border-box;}
  body{margin:0;color:var(--ink);
    background:radial-gradient(1200px 600px at 80% -10%,rgba(56,189,248,0.10),transparent 60%),
               radial-gradient(900px 500px at -10% 10%,rgba(57,135,229,0.08),transparent 55%),var(--plane);
    -webkit-font-smoothing:antialiased;}
  .wrap{max-width:1340px;margin:0 auto;padding:22px 22px 60px;}
  .top{display:flex;align-items:center;gap:16px;margin-bottom:20px;flex-wrap:wrap;}
  .brand{display:flex;align-items:center;gap:12px;}
  .reactor{width:42px;height:42px;border-radius:50%;
    background:radial-gradient(circle at 50% 50%,#eaffff 0%,var(--accent) 30%,#0e3a52 70%,#06141d 100%);
    box-shadow:0 0 0 2px rgba(56,189,248,0.25),0 0 20px rgba(56,189,248,0.55);position:relative;}
  .reactor::after{content:"";position:absolute;inset:11px;border-radius:50%;border:2px solid rgba(255,255,255,0.55);}
  .brand h1{font-size:19px;letter-spacing:4px;margin:0;font-weight:700;}
  .brand span{font-size:10.5px;letter-spacing:2px;color:var(--ink-3);font-weight:600;}
  .spacer{flex:1;}
  .pill{display:inline-flex;align-items:center;gap:7px;font-size:11.5px;font-weight:600;color:var(--ink-2);
    background:var(--surface);border:1px solid var(--line);padding:7px 12px;border-radius:999px;}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--good);box-shadow:0 0 8px var(--good);}
  .sec{font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:var(--ink-3);font-weight:700;margin:22px 2px 11px;}
  .card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:15px;}

  .streaks{display:grid;grid-template-columns:repeat(4,1fr);gap:13px;}
  @media(max-width:820px){.streaks{grid-template-columns:1fr 1fr;}}
  .sk{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:14px;position:relative;overflow:hidden;}
  .sk.key{border-color:rgba(56,189,248,0.4);background:linear-gradient(180deg,rgba(56,189,248,0.07),transparent);}
  .sk .lab{font-size:12px;color:var(--ink-2);display:flex;align-items:center;gap:6px;}
  .sk .big{font-size:34px;font-weight:700;line-height:1;margin:8px 0 2px;}
  .sk .big small{font-size:13px;color:var(--ink-2);font-weight:600;}
  .sk .sub{font-size:11px;color:var(--ink-3);}
  .keytag{position:absolute;top:11px;right:11px;font-size:9px;letter-spacing:.5px;font-weight:700;color:#06141d;background:var(--accent);padding:2px 7px;border-radius:999px;}

  .twelve{display:grid;grid-template-columns:repeat(4,1fr);gap:13px;}
  @media(max-width:980px){.twelve{grid-template-columns:1fr 1fr;}}
  @media(max-width:620px){.twelve{grid-template-columns:1fr;}}
  .twc{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:13px;}
  .twc .h{display:flex;align-items:center;gap:8px;font-size:12.5px;font-weight:700;margin-bottom:10px;}
  .twc .b{width:24px;height:24px;border-radius:7px;display:inline-flex;align-items:center;justify-content:center;
    font-size:10px;font-weight:800;color:#06141d;flex:0 0 auto;}
  .tt{display:flex;gap:9px;align-items:flex-start;font-size:12.8px;padding:7px 0;border-top:1px solid var(--line);}
  .tt:first-of-type{border-top:none;}
  .tt .cb{width:16px;height:16px;border-radius:5px;border:1.5px solid var(--ink-3);flex:0 0 auto;margin-top:1px;
    display:inline-flex;align-items:center;justify-content:center;}
  .tt .cb svg{width:10px;height:10px;visibility:hidden;}
  .tt.d .cb{background:var(--good);border-color:var(--good);}
  .tt.d .cb svg{visibility:visible;}
  .tt.d span:last-child{color:var(--ink-3);text-decoration:line-through;}
  .bonus{margin-top:12px;font-size:12.5px;color:var(--warn);font-weight:600;}
  .empty{color:var(--ink-3);font-size:12.5px;padding:8px 0;}

  .prog{display:grid;grid-template-columns:repeat(4,1fr);gap:13px;}
  @media(max-width:980px){.prog{grid-template-columns:1fr 1fr;}}
  .pt{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:14px;}
  .pt .lab{font-size:12px;color:var(--ink-2);}
  .pt .val{font-size:26px;font-weight:700;margin:6px 0 2px;}
  .pt .from{font-size:11px;color:var(--ink-3);margin-top:6px;}
  .spark{width:100%;height:34px;margin-top:8px;}

  .villa{display:grid;grid-template-columns:1.4fr 1fr;gap:16px;}
  @media(max-width:820px){.villa{grid-template-columns:1fr;}}
  .villa h3{margin:0;font-size:14px;}
  .pct{font-size:22px;font-weight:700;color:var(--accent);}
  .track{height:12px;background:var(--surface-2);border:1px solid var(--line);border-radius:8px;margin-top:10px;overflow:hidden;}
  .fill{height:100%;border-radius:8px;background:linear-gradient(90deg,var(--accent-2),var(--accent));box-shadow:0 0 14px rgba(56,189,248,0.4);}
  .villa .cap{font-size:12px;color:var(--ink-3);margin-top:9px;}
  .flags{border-left:1px solid var(--line);padding-left:16px;}
  @media(max-width:820px){.flags{border-left:none;padding-left:0;border-top:1px solid var(--line);padding-top:14px;}}
  .flag{display:flex;gap:9px;font-size:12.5px;margin-bottom:11px;align-items:flex-start;}
  .money{font-variant-numeric:tabular-nums;}

  .day{display:flex;flex-direction:column;}
  .slot{display:flex;gap:12px;padding:8px 0;border-top:1px solid var(--line);align-items:center;}
  .slot:first-child{border-top:none;}
  .slot .tm{font-size:12px;color:var(--ink-2);width:56px;flex:0 0 auto;font-variant-numeric:tabular-nums;}
  .slot .b{width:3px;align-self:stretch;border-radius:3px;flex:0 0 auto;}
  .slot .nm{font-size:13.5px;}
  .slot .ty{font-size:11px;color:var(--ink-3);}
  .b-run{background:var(--accent);}
  .b-work{background:#64748b;}
  .b-gym{background:var(--calm);}
  .b-meal{background:var(--warn);}
  .b-rev{background:var(--accent-2);}
  .b-cal{background:#a855c7;}
  .done .nm{color:var(--ink-3);text-decoration:line-through;}

  .lower{display:grid;grid-template-columns:0.8fr 1.1fr 1fr;gap:16px;align-items:start;}
  @media(max-width:980px){.lower{grid-template-columns:1fr;}}
  .ch{font-size:11px;letter-spacing:1.2px;text-transform:uppercase;color:var(--ink-2);margin:0 0 12px;font-weight:700;display:flex;align-items:center;}
  .ring-wrap{display:flex;align-items:center;gap:14px;}
  .ring{width:64px;height:64px;flex:0 0 auto;}
  .ring text{fill:var(--ink);font-size:12px;font-weight:700;}
  .ring-wrap .l b{font-size:15px;}
  .ring-wrap .l div{font-size:12px;color:var(--ink-2);}
  .mail .hd{display:flex;align-items:center;gap:8px;color:var(--ink-2);font-size:11px;margin-bottom:9px;border-bottom:1px solid var(--line);padding-bottom:8px;}
  .mail .hd b{color:var(--ink);}
  .mail p{margin:7px 0;line-height:1.45;font-size:12.8px;}
  .mail .row{display:flex;justify-content:space-between;font-size:12px;color:var(--ink-2);padding:2px 0;}
  .mail .row b{color:var(--ink);}
  .sob{border:1px solid rgba(45,212,191,0.28);background:linear-gradient(180deg,rgba(45,212,191,0.06),rgba(45,212,191,0.01));}
  .sob .ch{color:#7fe9d8;}
  .sob .big{font-size:30px;font-weight:700;color:#bff5ec;}
  .sob .u{font-size:12px;color:var(--ink-2);margin-left:6px;}
  .lock{font-size:10px;color:#7fe9d8;background:rgba(45,212,191,0.12);padding:3px 8px;border-radius:999px;font-weight:600;margin-left:auto;}
  .sob .ins{font-size:11.5px;color:var(--ink-2);margin-top:10px;line-height:1.5;}
  .foot{margin-top:26px;text-align:center;color:var(--ink-3);font-size:11.5px;}
  .foot b{color:var(--ink-2);}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div class="brand"><div class="reactor"></div><div><h1>JARVIS</h1><br><span>PROGRESS COCKPIT</span></div></div>
    <div class="spacer"></div>
    <span class="pill" id="pill-loc">📍 …</span>
    <span class="pill"><span class="dot"></span> <span id="pill-day">Day — with Jarvis</span></span>
    <button class="pill" id="talk-btn" style="cursor:pointer;border:none;font:inherit"
      title="Live, interruptible conversation — headphones recommended">🎙 Talk to Jarvis</button>
    <button class="pill" id="interpret-btn" style="cursor:pointer;border:none;font:inherit"
      title="Live interpreter — speak English or Portuguese, Jarvis carries it both ways and can explain concepts">🌐 Interpret EN⇄PT</button>
    <button class="pill" id="support-btn" style="cursor:pointer;border:none;font:inherit"
      title="A private space to talk — just you and Jarvis, nothing touches the board">💙 Support</button>
  </div>

  <div class="sec">Streaks · consecutive days</div>
  <div class="streaks" id="streaks"></div>

  <div class="sec" id="twelve-sec">Today's Focus · built by Jarvis from your board</div>
  <div class="twelve" id="twelve"></div>

  <div class="sec">Body · progress</div>
  <div class="prog" id="body"></div>

  <div class="sec">Dubai villa · Sobha Elwood SEL-V 202</div>
  <div class="card villa" id="villa"></div>

  <div class="sec">Today · your hour-by-hour</div>
  <div class="card day" id="timeline"></div>

  <div class="sec">Right now</div>
  <div class="lower">
    <div class="card">
      <p class="ch">Today's Focus</p>
      <div class="ring-wrap">
        <svg class="ring" viewBox="0 0 36 36">
          <circle cx="18" cy="18" r="15.5" fill="none" stroke="#242a35" stroke-width="4"/>
          <circle id="ring-fill" cx="18" cy="18" r="15.5" fill="none" stroke="#22c55e" stroke-width="4"
                  stroke-linecap="round" stroke-dasharray="97.4" stroke-dashoffset="97.4" transform="rotate(-90 18 18)"/>
          <text id="ring-text" x="18" y="22.5" text-anchor="middle">–/12</text>
        </svg>
        <div class="l"><b id="ring-label">Loading…</b><div id="ring-sub">Any size of day counts</div></div>
      </div>
    </div>
    <div class="card">
      <p class="ch">Tonight's note to Kiefer · 21:00</p>
      <div class="mail">
        <div class="hd">✉️ <b>To: Kiefer</b> · daily summary</div>
        <p id="kiefer-preview">…</p>
        <div class="row"><span>Run</span><b id="k-run">—</b></div>
        <div class="row"><span>Workout</span><b id="k-workout">—</b></div>
        <div class="row"><span>Today's Focus</span><b id="k-twelve">—</b></div>
      </div>
    </div>
    <div class="card sob">
      <p class="ch">Sobriety <span class="lock">🔒 private</span></p>
      <div id="sob-count"><span class="big">—</span></div>
      <div class="ins">No scoreboard, no shame — just support. This panel never leaves this page.</div>
    </div>
  </div>

  <div class="foot">JARVIS · Progress Cockpit — <b>everything in one glance.</b> Live from your backend · refreshed <span id="gen">–</span></div>
</div>
<script>
const TICK = '<svg viewBox="0 0 12 12"><path d="M1 6l3.2 3.2L11 2" fill="none" stroke="#06141d" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function spark(series, color){
  if(!series || series.length < 2) return '';
  const min = Math.min(...series), max = Math.max(...series), span = (max-min) || 1;
  const pts = series.map((v,i) => `${(i/(series.length-1)*100).toFixed(1)},${(30-(v-min)/span*24+2).toFixed(1)}`).join(' ');
  const last = pts.split(' ').pop();
  return `<svg class="spark" viewBox="0 0 100 34" preserveAspectRatio="none">
    <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="${last.split(',')[0]}" cy="${last.split(',')[1]}" r="3" fill="${color}"/></svg>`;
}

function render(d){
  document.getElementById('pill-loc').textContent = '📍 ' + d.location;
  document.getElementById('pill-day').textContent = 'Day ' + d.day_with_jarvis + ' with Jarvis';
  document.getElementById('gen').textContent = d.generated_at;

  // Daily-doable habits stay streaks; physical training is monthly and
  // recovery-aware — a rest day is never a broken anything (§11).
  const meta = {twelve:['✅ Today’s Focus cleared', true],
                portuguese:['🇧🇷 Portuguese', false], meals:['🥗 Meals on-plan', false]};
  const chips = Object.entries(meta).map(([k,[label,key]]) => {
    const s = d.streaks[k] || {current:0, best:0, done_today:false};
    const today = s.done_today ? '✓ done today' : 'not logged today';
    return `<div class="sk${key?' key':''}">${key?'<span class="keytag">KEYSTONE</span>':''}
      <div class="lab">${label}</div><div class="big">${s.current} <small>days</small></div>
      <div class="sub">${today} · best: ${s.best}</div></div>`;
  });
  const a = d.activity || {runs:0, workouts:0, recovery_days:0};
  chips.push(`<div class="sk"><div class="lab">🏃 🏋️ This month</div>
    <div class="big">${a.runs}<small> runs</small> · ${a.workouts}<small> workouts</small></div>
    <div class="sub">${a.recovery_days} recovery day${a.recovery_days===1?'':'s'} — rest is training</div></div>`);
  const rh = d.rhythm || {};
  if (rh.water_target_ml) chips.push(`<div class="sk"><div class="lab">💧 Water · 🚶 Moves</div>
    <div class="big">${((rh.water_ml||0)/1000).toFixed(1)}<small>L / ${(rh.water_target_ml/1000).toFixed(1)}L</small> · ${rh.movements||0}<small> moves</small></div>
    <div class="sub">${rh.woke_at ? 'up at ' + esc(rh.woke_at) : 'wake not logged'}</div></div>`);
  document.getElementById('streaks').innerHTML = chips.join('');

  const t = d.twelve;
  document.getElementById('twelve-sec').textContent =
    `Today's Focus · built by Jarvis from your board (${t.done} done)`;
  document.getElementById('twelve').innerHTML = t.by_company.map(c => `
    <div class="twc"><div class="h"><span class="b" style="background:${c.gradient}">${c.initials}</span>${esc(c.name)}</div>
    ${c.tasks.length ? c.tasks.map(x => `<div class="tt${x.done?' d':''}"><span class="cb">${TICK}</span><span>${esc(x.title)}</span></div>`).join('')
                     : '<div class="empty">No tasks selected yet — say "plan my day" to Jarvis.</div>'}
    </div>`).join('') + (t.bonus ? `<div class="bonus">🎁 Bonus unlocked: ${esc(t.bonus.title)}</div>` : '');

  const total = t.total || 12;
  document.getElementById('ring-text').textContent = `${t.done}/${total}`;
  document.getElementById('ring-fill').setAttribute('stroke-dashoffset', (97.4*(1-(total?t.done/total:0))).toFixed(1));
  document.getElementById('ring-label').textContent = `${t.done} of ${total} done`;
  document.getElementById('ring-sub').textContent = (t.done>=total && total) ? 'List cleared — outstanding! 🎉' : 'Any size of day counts';

  const b = d.body; const tiles = [];
  if(b.weight) tiles.push(`<div class="pt"><div class="lab">Weight</div><div class="val">${b.weight.latest.toFixed(1)} kg</div>
    ${spark(b.weight.series,'#2dd4bf')}<div class="from">from ${b.weight.first.toFixed(1)} kg · Apple Health</div></div>`);
  if(b.run_pace) tiles.push(`<div class="pt"><div class="lab">5km time</div><div class="val">${b.run_pace.latest_min.toFixed(1)} min</div>
    ${spark(b.run_pace.series,'#38bdf8')}<div class="from">best ${b.run_pace.best_min.toFixed(1)} · first ${b.run_pace.first_min.toFixed(1)}</div></div>`);
  if(b.weight_target) tiles.push(`<div class="pt"><div class="lab">Target</div><div class="val">${esc(b.weight_target)}</div><div class="from">weight goal</div></div>`);
  if(b.body_fat) tiles.push(`<div class="pt"><div class="lab">Body fat</div><div class="val">${esc(b.body_fat)}</div><div class="from">latest scan</div></div>`);
  if(b.lean_muscle) tiles.push(`<div class="pt"><div class="lab">Lean muscle</div><div class="val">${esc(b.lean_muscle)}</div><div class="from">latest scan</div></div>`);
  if(b.strength) tiles.push(`<div class="pt"><div class="lab">Strength</div><div class="val">${esc(b.strength)}</div><div class="from">gym benchmarks</div></div>`);
  if(!tiles.length) tiles.push(`<div class="pt"><div class="lab">Baselines pending</div>
    <div class="val">—</div><div class="from">${esc(b.weight_note || 'Body scan + 5km benchmark to come — data lands here automatically.')}</div></div>`);
  document.getElementById('body').innerHTML = tiles.join('');

  const v = d.villa;
  document.getElementById('villa').innerHTML = v.available ? `
    <div><div style="display:flex;justify-content:space-between;align-items:center"><h3>Paid off</h3><span class="pct">${v.pct}%</span></div>
      <div class="track"><div class="fill" style="width:${v.pct}%"></div></div>
      <div class="cap money">${esc(v.paid_label)}</div>
      <div class="cap money">${esc(v.price_label)} · next: ${esc(v.next_demand)}</div></div>
    <div class="flags">${v.flags.map(f => `<div class="flag"><span>⚠️</span><div class="money">${esc(f)}</div></div>`).join('')}</div>`
    : '<div class="cap">Villa tracker connects at Milestone 2 data load.</div>';

  document.getElementById('timeline').innerHTML = d.today.map(s => `
    <div class="slot${s.done?' done':''}"><div class="tm">${esc(s.time)}</div><div class="b b-${esc(s.type)}"></div>
    <div><div class="nm">${esc(s.title)}</div></div></div>`).join('');

  document.getElementById('kiefer-preview').textContent = '"' + d.kiefer.preview + '"';
  document.getElementById('k-run').textContent = d.kiefer.run ? '✓ done' : '—';
  document.getElementById('k-workout').textContent = d.kiefer.workout ? '✓ done' : '—';
  document.getElementById('k-twelve').textContent = d.kiefer.twelve;

  document.getElementById('sob-count').innerHTML = d.sobriety.days !== null
    ? `<span class="big">${d.sobriety.days}</span><span class="u">days strong</span>`
    : `<span class="big">—</span><span class="u">check in with Jarvis tonight</span>`;
}

fetch('DATA_URL').then(r => r.json()).then(render)
  .catch(() => document.getElementById('pill-day').textContent = 'connection hiccup — refresh');
setInterval(() => fetch('DATA_URL').then(r => r.json()).then(render).catch(()=>{}), 60000);

// Live voice (Build Slice: Voice Access): open a realtime, interruptible
// session with Jarvis in the browser — his real voice, barge-in and all.
// Two modes share the plumbing: assistant (Talk) and EN⇄PT interpreter.
async function openLive(btnId, mode, idle, live) {
  const btn = document.getElementById(btnId);
  if (document.querySelector('elevenlabs-convai')) { return; }  // already live
  btn.textContent = 'Connecting…';
  try {
    const res = await fetch('DATA_URL'.replace(/\/data$/, '/voice-url') + '?mode=' + mode, {method:'POST'});
    const body = await res.json();
    if (body.error) { btn.textContent = idle; alert(body.error); return; }
    if (!window.customElements.get('elevenlabs-convai')) {
      await new Promise((ok, err) => {
        const s = document.createElement('script');
        s.src = 'https://unpkg.com/@elevenlabs/convai-widget-embed';
        s.async = true; s.onload = ok; s.onerror = err;
        document.body.appendChild(s);
      });
    }
    const widget = document.createElement('elevenlabs-convai');
    widget.setAttribute('signed-url', body.url);
    document.body.appendChild(widget);
    btn.textContent = live;
  } catch (e) {
    btn.textContent = idle;
    alert('Could not open the live session — try again in a moment.');
  }
}
document.getElementById('talk-btn').addEventListener('click',
  () => openLive('talk-btn', 'assistant', '🎙 Talk to Jarvis', '🎙 Live — talk away'));
document.getElementById('support-btn').addEventListener('click',
  () => openLive('support-btn', 'support', '💙 Support', '💙 Here with you'));

// The interpreter runs PUSH-TO-TALK (Paul's ask, 31 Jul): the mic is dead
// until the big button is held, so thinking pauses can never be cut off —
// you finish, you let go, Jarvis translates the whole thought.
async function openInterpreter() {
  const btn = document.getElementById('interpret-btn');
  if (window.__jarvisPtt) { return; }
  btn.textContent = 'Connecting…';
  try {
    const res = await fetch('DATA_URL'.replace(/\/data$/, '/voice-url') + '?mode=interpreter', {method:'POST'});
    const body = await res.json();
    if (body.error) { btn.textContent = '🌐 Interpret EN⇄PT'; alert(body.error); return; }
    const { Conversation } = await import('https://cdn.jsdelivr.net/npm/@elevenlabs/client/+esm');
    const conv = await Conversation.startSession({ signedUrl: body.url });
    conv.setMicMuted(true);   // silent until the button is held
    window.__jarvisPtt = conv;

    const bar = document.createElement('div');
    bar.id = 'ptt-bar';
    bar.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:99;display:flex;gap:10px;'
      + 'align-items:center;padding:14px 16px calc(14px + env(safe-area-inset-bottom));'
      + 'background:rgba(10,16,24,.96);backdrop-filter:blur(8px);border-top:1px solid #22303f';
    bar.innerHTML = `
      <button id="ptt-hold" style="flex:1;padding:20px;border-radius:16px;border:none;font-size:17px;
        font-weight:600;background:#2f81f7;color:#fff;cursor:pointer;-webkit-user-select:none;user-select:none;
        touch-action:none">🎙 Hold to speak · segure para falar</button>
      <button id="ptt-end" style="padding:20px 16px;border-radius:16px;border:1px solid #2a3644;
        background:transparent;color:#8fa0b3;font-size:15px;cursor:pointer">End</button>`;
    document.body.appendChild(bar);

    const hold = document.getElementById('ptt-hold');
    const down = e => { e.preventDefault(); conv.setMicMuted(false);
      hold.style.background = '#dc2626'; hold.textContent = '🔴 Listening — let go when you’re done'; };
    const up = e => { e.preventDefault(); conv.setMicMuted(true);
      hold.style.background = '#2f81f7'; hold.textContent = '🎙 Hold to speak · segure para falar'; };
    hold.addEventListener('mousedown', down);
    hold.addEventListener('touchstart', down, {passive:false});
    hold.addEventListener('mouseup', up);
    hold.addEventListener('mouseleave', up);
    hold.addEventListener('touchend', up);
    hold.addEventListener('touchcancel', up);
    document.getElementById('ptt-end').addEventListener('click', async () => {
      try { await conv.endSession(); } catch (e) {}
      bar.remove(); window.__jarvisPtt = null;
      btn.textContent = '🌐 Interpret EN⇄PT';
    });
    btn.textContent = '🌐 Interpreting';
  } catch (e) {
    btn.textContent = '🌐 Interpret EN⇄PT';
    alert('Could not open the live session — try again in a moment.');
  }
}
document.getElementById('interpret-btn').addEventListener('click', openInterpreter);
</script>
</body>
</html>"""


def render_page(data_url: str) -> str:
    return COCKPIT_HTML.replace("DATA_URL", data_url)
