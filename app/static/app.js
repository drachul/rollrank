let state = null;
const initialParams = new URLSearchParams(window.location.search);
const supportedViews = ["dashboard", "heats", "standings", "setup"];
let activeView = supportedViews.includes(initialParams.get("view")) ? initialParams.get("view") : "dashboard";
let activeDay = 1;
let activeTournamentId = Number(initialParams.get("tournament")) || null;
let kioskMode = initialParams.get("display") === "kiosk";
let liveEventSource = null;
let liveEventTournamentId = null;
let kioskRefreshFailed = false;
let kioskLastUpdated = null;
let kioskUsesBrowserFullscreen = false;
if (kioskMode) activeView = "dashboard";

const landingPage = document.querySelector("#landing-page");
const workspaceShell = document.querySelector("#workspace-shell");
const tournamentIndexList = document.querySelector("#tournament-index-list");
const app = document.querySelector("#app");
const toast = document.querySelector("#toast");
const tournamentSwitcher = document.querySelector("#tournament-switcher");
const tournamentDialog = document.querySelector("#new-tournament-dialog");

const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;").replaceAll("'", "&#039;");

const ordinal = (value) => {
  const number = Number(value);
  if (number % 100 >= 11 && number % 100 <= 13) return `${number}th`;
  return `${number}${({1:"st",2:"nd",3:"rd"})[number % 10] || "th"}`;
};

const placeLabel = (place, live = false) => place == null ? "Pending" : `${ordinal(place)}${live ? "*" : ""}`;
const tierLabel = {bye: "Final", preliminary: "Preliminary", wildcard: "Wildcard"};
const tierClass = (tier) => tier ? `tier-${tier}` : "";

function notify(message, isError = false) {
  toast.textContent = message;
  toast.className = `toast show${isError ? " error" : ""}`;
  window.setTimeout(() => toast.className = "toast", 3200);
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {"Content-Type": "application/json", ...(options.headers || {})},
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || "The request could not be completed.");
    error.status = response.status;
    Object.assign(error, data);
    throw error;
  }
  return data;
}

function reportUrl() {
  return `/api/tournaments/${state.competition.id}/report.pdf`;
}

function syncTournamentChrome() {
  activeTournamentId = state.competition.id;
  tournamentSwitcher.innerHTML = state.tournaments.map((tournament) => `<option value="${tournament.id}" ${tournament.id === activeTournamentId ? "selected" : ""}>${escapeHtml(tournament.name)} · ${tournament.completedHeats}/${tournament.totalHeats}</option>`).join("");
  document.title = `${state.competition.name} · RollRank`;
  const url = new URL(window.location.href);
  url.searchParams.set("tournament", activeTournamentId);
  window.history.replaceState({}, "", url);
}

function applyState(nextState) {
  state = nextState;
  activeTournamentId = state.competition.id;
  if (activeDay !== "championship") activeDay = Math.min(activeDay, state.competition.days);
  kioskRefreshFailed = false;
  kioskLastUpdated = new Date();
  syncTournamentChrome();
  render();
}

function kioskTimestamp() {
  if (!kioskLastUpdated) return "Connecting…";
  return `Updated ${kioskLastUpdated.toLocaleTimeString([], {hour:"numeric", minute:"2-digit", second:"2-digit"})}`;
}

function activeViewIsLive() {
  return kioskMode || activeView === "dashboard" || activeView === "standings";
}

function markLiveDisconnected() {
  kioskRefreshFailed = true;
  const liveStatus = document.querySelector(".kiosk-live-status");
  if (liveStatus) {
    liveStatus.classList.add("stale");
    liveStatus.innerHTML = "<i></i> Reconnecting…";
  }
}

function startLiveUpdates() {
  if (!activeViewIsLive() || !activeTournamentId || document.visibilityState === "hidden") {
    stopLiveUpdates();
    return;
  }
  if (liveEventSource && liveEventTournamentId === activeTournamentId) return;
  stopLiveUpdates();
  const tournamentId = activeTournamentId;
  const source = new EventSource(`/api/tournaments/${tournamentId}/events`);
  liveEventSource = source;
  liveEventTournamentId = tournamentId;
  source.addEventListener("state", (event) => {
    if (source !== liveEventSource) return;
    try { applyState(JSON.parse(event.data)); }
    catch (_error) { markLiveDisconnected(); }
  });
  source.addEventListener("tournament-deleted", () => {
    if (source !== liveEventSource) return;
    stopLiveUpdates();
    loadState(null);
  });
  source.onerror = () => {
    if (source === liveEventSource) markLiveDisconnected();
  };
}

function stopLiveUpdates() {
  if (liveEventSource) liveEventSource.close();
  liveEventSource = null;
  liveEventTournamentId = null;
}

async function setKioskMode(enabled, requestBrowserFullscreen = false) {
  kioskMode = enabled;
  activeView = "dashboard";
  document.body.classList.toggle("kiosk-mode", enabled);
  const url = new URL(window.location.href);
  url.searchParams.set("view", "dashboard");
  if (enabled) url.searchParams.set("display", "kiosk");
  else url.searchParams.delete("display");
  window.history.replaceState({}, "", url);

  render();
  if (enabled) {
    if (requestBrowserFullscreen && document.documentElement.requestFullscreen && !document.fullscreenElement) {
      kioskUsesBrowserFullscreen = true;
      try { await document.documentElement.requestFullscreen(); }
      catch (_error) { kioskUsesBrowserFullscreen = false; }
    }
  } else {
    if (document.fullscreenElement && document.exitFullscreen) {
      try { await document.exitFullscreen(); } catch (_error) { /* The page view still exits kiosk mode. */ }
    }
    kioskUsesBrowserFullscreen = false;
  }
}

async function loadState(tournamentId = activeTournamentId, allowFallback = true) {
  try {
    const query = tournamentId ? `?tournamentId=${tournamentId}` : "";
    applyState(await api(`/api/state${query}`));
  } catch (error) {
    if (allowFallback && tournamentId && error.status === 404) {
      activeTournamentId = null;
      return loadState(null, false);
    }
    if (error.status === 404) {
      app.innerHTML = `<section class="empty-state workspace-empty"><img src="/static/marble-logo.png" alt="" width="64" height="64"><h1>No tournaments yet</h1><p>Create your first tournament to configure racers, generate heats, and start scoring.</p><button class="primary-button" data-new-tournament>+ Create tournament</button></section>`;
    } else {
      app.innerHTML = `<section class="empty-state"><h1>RollRank is unavailable</h1><p>${escapeHtml(error.message)}</p><button class="primary-button" data-retry>Try again</button></section>`;
    }
  }
}

function marble(color, size = "normal") {
  return `<i class="marble ${size}" style="--marble-color:${escapeHtml(color)}" aria-hidden="true"></i>`;
}

function tournamentStatus(tournament) {
  if (tournament.finalComplete) return {label:"Complete", className:"complete"};
  if (tournament.completedHeats === tournament.totalHeats) return {label:"Final ready", className:"final-ready"};
  if (tournament.completedHeats > 0) return {label:"In progress", className:""};
  return {label:"Not started", className:"not-started"};
}

function currentTournamentStatus() {
  return tournamentStatus({...state.competition, finalComplete:state.championship.final.complete});
}

function originBadge(entry) {
  const stage = entry.originStage;
  if (!stage) return "";
  const labels = {
    "bye": `Bye · Round ${entry.originRound}`,
    "stage-skip": "Advanced · small field",
    "staging-round": `From Round ${entry.originRound}`,
    "wildcard": "From Wildcard",
    "preliminary": "From Preliminary",
  };
  return `<span class="origin-badge">${escapeHtml(labels[stage] || "Advanced")}</span>`;
}

function renderTournamentIndex(tournaments) {
  if (!tournaments.length) {
    tournamentIndexList.innerHTML = `<div class="index-empty"><img src="/static/marble-logo.png" alt=""><h2>No tournaments yet</h2><p>Create your first tournament, add the racers, and RollRank will build the opening heat schedule.</p><button type="button" class="primary-button" data-new-tournament>+ Create your first tournament</button></div>`;
    return;
  }
  tournamentIndexList.innerHTML = `<div class="tournament-index-grid">${tournaments.map((tournament) => {
    const status = tournamentStatus(tournament);
    const progress = tournament.totalHeats ? Math.round(tournament.completedHeats / tournament.totalHeats * 100) : 0;
    const leader = tournament.leader
      ? `<div class="index-leader">${marble(tournament.leader.color, "small")}<div><small>Live leader</small><strong>${escapeHtml(tournament.leader.name)}</strong></div><b>${tournament.leader.wins} win${tournament.leader.wins === 1 ? "" : "s"}</b></div>`
      : `<div class="index-leader empty">No results entered yet</div>`;
    return `<article class="tournament-index-card">
      <div class="index-card-top"><span class="index-status ${status.className}">${status.label}</span><span class="index-progress-count">${tournament.completedHeats}/${tournament.totalHeats} heats</span></div>
      <h2>${escapeHtml(tournament.name)}</h2>
      <div class="index-progress-label"><span>Heat progress</span><b>${progress}%</b></div>
      <div class="index-card-progress" aria-label="${progress}% of heats complete"><i style="width:${progress}%"></i></div>
      ${leader}
      <a class="index-dashboard-button" href="/workspace?tournament=${tournament.id}">Open dashboard <span aria-hidden="true">→</span></a>
    </article>`;
  }).join("")}</div>`;
}

async function loadTournamentIndex() {
  try {
    const result = await api("/api/tournaments");
    renderTournamentIndex(result.tournaments);
  } catch (error) {
    tournamentIndexList.innerHTML = `<div class="index-empty"><h2>Tournaments are unavailable</h2><p>${escapeHtml(error.message)}</p><button type="button" class="secondary-button" data-index-retry>Try again</button></div>`;
  }
}

function progressPercent() {
  const { completedHeats, totalHeats } = state.competition;
  return totalHeats ? Math.round(completedHeats / totalHeats * 100) : 0;
}

function viewHeader(eyebrow, title, description, action = "") {
  return `<header class="view-heading"><div><p class="eyebrow">${eyebrow}</p><h1>${title}</h1><p>${description}</p></div>${action}</header>`;
}

function fireworksMarkup() {
  return `<div class="kiosk-fireworks" aria-hidden="true">
    <i style="--x:12%;--y:17%;--delay:0s;--color:#f4c542"></i><i style="--x:31%;--y:9%;--delay:.75s;--color:#5ba1ff"></i><i style="--x:52%;--y:18%;--delay:1.35s;--color:#ff6b8a"></i><i style="--x:72%;--y:8%;--delay:.35s;--color:#6ee7b7"></i><i style="--x:89%;--y:21%;--delay:1.05s;--color:#c4a7ff"></i><i style="--x:22%;--y:53%;--delay:1.7s;--color:#ff8f5b"></i><i style="--x:82%;--y:58%;--delay:2s;--color:#f4c542"></i>
  </div>`;
}

function renderDashboardFinalSummary() {
  const c = state.competition;
  const finalStage = state.championship.final;
  const champion = finalStage.champion;
  const entries = finalStage.heat.entries;
  const status = currentTournamentStatus();
  const dnfCount = entries.filter((entry) => entry.finish === 0).length;
  const dnfPlace = finalDnfPlace(entries);

  return `<section class="dashboard-final-summary" aria-label="Completed tournament summary">
    ${fireworksMarkup()}
    <section class="dashboard-final-hero">
      <div class="hero-copy dashboard-final-hero-copy">
        <div class="dashboard-status-row"><span class="status-chip ${status.className}"><i></i>${status.label}</span><button type="button" class="kiosk-launch-button" data-enter-kiosk><span aria-hidden="true">⛶</span> Fullscreen display</button></div>
        <p class="eyebrow">Official final result</p>
        <h1>${escapeHtml(champion.name)} is the <em>champion.</em></h1>
        <p>${escapeHtml(c.name)} is complete. Review the finalist field, podium, and official finishing order below.</p>
      </div>
      <aside class="dashboard-champion-card" aria-label="Tournament champion">
        <span class="dashboard-champion-trophy" aria-hidden="true">🏆</span>
        ${marble(champion.color)}
        <small>RollRank champion</small>
        <strong>${escapeHtml(champion.name)}</strong>
      </aside>
    </section>
    <section class="panel dashboard-final-lineup">
      <div class="panel-heading"><div><p class="eyebrow">The final</p><h2>Final race lineup</h2></div><button class="text-button" data-view="heats" data-day="championship">View championship details →</button></div>
      <div class="dashboard-final-racers">
        ${entries.map((entry) => `<article class="${entry.finish === 0 ? "dnf" : ""}">
          ${marble(entry.color)}
          <div><strong>${escapeHtml(entry.name)}</strong>${originBadge(entry)}</div>
          <b class="dashboard-final-finish">${finalFinishLabel(entry.finish, entries)}</b>
        </article>`).join("")}
      </div>
      ${dnfCount ? `<p class="dashboard-final-note">${dnfCount} DNF${dnfCount === 1 ? "" : "s"} tied for ${ordinal(dnfPlace)} place.</p>` : ""}
    </section>
    ${renderFinalCelebration(finalStage)}
  </section>`;
}

function renderDashboard() {
  if (state.championship.final.complete) return renderDashboardFinalSummary();
  const c = state.competition;
  const status = currentTournamentStatus();
  const nextHeat = state.days.flatMap((day) => day.heats).find((heat) => !heat.complete);
  const topRacers = state.standings.slice(0, 5);
  return `
    <section class="dashboard-hero">
      <div class="hero-copy">
        <div class="dashboard-status-row"><span class="status-chip ${status.className}"><i></i>${status.label}</span><button type="button" class="kiosk-launch-button" data-enter-kiosk><span aria-hidden="true">⛶</span> Fullscreen display</button></div>
        <h1>Every heat. Every point.<br><em>One champion.</em></h1>
        <p>Run the schedule, enter finishing positions, and watch the championship bracket take shape in real time.</p>
      </div>
      <aside class="summary-card">
        <p class="eyebrow light">Tournament snapshot</p>
        <div class="summary-lead"><strong>${c.days}</strong><span>race<br>rounds</span></div>
        <div class="summary-pills"><span><b>${c.heatsPerRacerPerDay}</b> heats / racer / round</span><span><b>${c.heatsPerDay}</b> total heats / round</span><span><b>${c.racersPerHeat}</b> racers / heat</span><span><b>${c.marblesPerHeat}/${c.maxMarblesPerHeat}</b> marbles / heat</span><span><b>${c.maxFinalRacers}</b> max finalists</span></div>
        <div class="progress-label"><span>Results entered</span><b>${c.completedHeats} of ${c.totalHeats}</b></div>
        <div class="progress-track"><i style="width:${progressPercent()}%"></i></div>
      </aside>
    </section>
    <section class="dashboard-grid">
      <article class="panel">
        <div class="panel-heading"><div><p class="eyebrow">Race queue</p><h2>${nextHeat ? `${nextHeat.started ? "In Progress" : "Next"}: Round ${nextHeat.day}, Heat ${nextHeat.heatNumber}` : "All round heats complete"}</h2></div><button class="text-button" data-view="heats">View rounds →</button></div>
        ${nextHeat ? `<div class="next-heat">
          <div class="heat-flag"><small>Race</small><strong>#${nextHeat.globalNumber}</strong></div>
          <div class="racer-preview">${nextHeat.entries.map((entry) => `<span>${marble(entry.color, "small")}<b>${escapeHtml(entry.name)}</b></span>`).join("")}</div>
          ${nextHeat.started === false ? `<button class="primary-button compact" data-start-heat="${nextHeat.id}">Start heat</button>` : `<button class="primary-button compact" data-score-heat="${nextHeat.id}">Score heat</button>`}
        </div>` : `<div class="completion-callout"><div class="trophy">★</div><div><strong>Championship bracket underway</strong><p>Wildcard, preliminary, and final heats build automatically as each stage finishes.</p></div><button class="primary-button compact" data-view="heats" data-day="championship">Open bracket</button></div>`}
      </article>
      <article class="panel standings-preview">
        <div class="panel-heading"><div><p class="eyebrow">Live table</p><h2>Tournament standings</h2></div></div>
        ${topRacers.map((racer) => `<div class="standing-row"><span class="rank">${racer.rank}</span>${marble(racer.color, "small")}<strong>${escapeHtml(racer.name)}</strong>${standingStatBadgesMarkup(racer)}</div>`).join("")}
        <button class="wide-link" data-view="standings">View full standings <span>→</span></button>
      </article>
    </section>`;
}

function renderKioskFinalDashboard() {
  const c = state.competition;
  const finalStage = state.championship.final;
  const status = currentTournamentStatus();
  const liveStatus = kioskRefreshFailed ? "Reconnecting…" : kioskTimestamp();
  const entries = finalStage.heat.entries;
  const finishers = entries.filter((entry) => entry.finish > 0).sort((first, second) => first.finish - second.finish);
  const dnfs = entries.filter((entry) => entry.finish === 0);
  const dnfPlace = finalDnfPlace(entries);
  const placedRacers = new Map(finishers.map((entry) => [entry.finish, entry]));
  const podiumMeta = {
    1: {className:"gold", icon:"🏆", label:"Gold trophy"},
    2: {className:"silver", icon:"🥈", label:"Silver trophy"},
    3: {className:"bronze", icon:"🥉", label:"Bronze trophy"},
  };
  const podiumOrder = [2, 1, 3].filter((position) => finalStage.complete ? placedRacers.has(position) : position <= entries.length);
  const remainingEntries = finalStage.complete
    ? [...finishers.filter((entry) => entry.finish > 3).map((entry) => ({finish:entry.finish, racer:entry, dnf:false})), ...dnfs.map((entry) => ({finish:0, racer:entry, dnf:true}))]
    : Array.from({length:Math.max(0, entries.length - 3)}, (_, index) => ({finish:index + 4, racer:null, dnf:false}));
  const podium = podiumOrder.map((position) => {
    const racer = placedRacers.get(position);
    const meta = podiumMeta[position];
    return `<article class="kiosk-podium-slot ${meta.className} ${racer ? "filled" : "pending"}">
      <div class="kiosk-podium-award"><span role="img" aria-label="${meta.label}">${meta.icon}</span><b>${ordinal(position)}</b></div>
      <div class="kiosk-podium-person">${racer ? `${marble(racer.color)}<strong>${escapeHtml(racer.name)}</strong>` : `<i aria-hidden="true">?</i><strong>Awaiting result</strong><small>${ordinal(position)} place</small>`}</div>
    </article>`;
  }).join("");
  const remaining = remainingEntries.map(({finish, racer, dnf}) => {
    const label = racer ? finalFinishLabel(finish, entries) : `${ordinal(finish)} place`;
    return `<article class="kiosk-final-place ${racer ? "filled" : "pending"}${dnf ? " dnf" : ""}"><span>${dnf ? `T${dnfPlace}` : finish}</span>${racer ? marble(racer.color, "small") : `<i aria-hidden="true">?</i>`}<strong>${racer ? escapeHtml(racer.name) : "Awaiting result"}</strong><small>${label}</small></article>`;
  }).join("");
  const fireworks = finalStage.complete ? fireworksMarkup() : "";

  return `<section class="kiosk-dashboard kiosk-final-dashboard${finalStage.complete ? " complete" : ""}" aria-label="Live tournament final dashboard">
    ${fireworks}
    <header class="kiosk-header">
      <a class="kiosk-brand" href="/" aria-label="RollRank home"><img src="/static/marble-logo.png" alt="" width="48" height="48"><span><strong>RollRank</strong><small>Live tournament final</small></span></a>
      <div class="kiosk-title"><p>${status.label}</p><h1>${escapeHtml(c.name)}</h1></div>
      <div class="kiosk-controls"><span class="kiosk-live-status${kioskRefreshFailed ? " stale" : ""}"><i></i>${liveStatus}</span><button type="button" data-exit-kiosk aria-label="Exit fullscreen display">Exit <span aria-hidden="true">×</span></button></div>
    </header>
    <section class="kiosk-final-lineup">
      <header><div><p class="kiosk-card-label">${finalStage.complete ? "The final" : (finalStage.heat.started ? "In progress" : "Up next")}</p><h2>${finalStage.complete ? `${escapeHtml(finalStage.champion.name)} is the champion` : "Championship: Final"}</h2></div><span>${finalStage.complete ? "Official result" : "Awaiting final result"}</span></header>
      <div class="kiosk-final-racers">${entries.map((entry) => `<article>${marble(entry.color)}<div><strong>${escapeHtml(entry.name)}</strong>${originBadge(entry)}</div></article>`).join("")}</div>
    </section>
    <section class="kiosk-final-results">
      <header><div><p class="kiosk-card-label">${finalStage.complete ? "Official result" : "Placement board"}</p><h2>${finalStage.complete ? "Final podium" : "Podium spots"}</h2></div><span>${finalStage.complete ? "🏆 Final complete" : "Results will appear here automatically"}</span></header>
      <div class="kiosk-final-podium">${podium}</div>
      ${remainingEntries.length ? `<div class="kiosk-remaining-heading"><strong>Remaining places</strong><span>${finalStage.complete ? (dnfs.length ? `${dnfs.length} DNF${dnfs.length === 1 ? "" : "s"} tied for ${ordinal(dnfPlace)}` : "Final order") : "Waiting to be filled"}</span></div><div class="kiosk-final-places">${remaining}</div>` : ""}
      <footer><span>${finalStage.complete ? `${escapeHtml(finalStage.champion.name)} takes the RollRank title` : "Submit the final result from the Rounds tab"}</span></footer>
    </section>
  </section>`;
}

function racerStatBadges(racer) {
  const tiers = racer.dayChampionshipTiers || [];
  const stats = [
    {label: "W", title: "Wins", count: racer.wins, live: racer.liveRoundLeader, liveAlreadyCounted: false},
    {label: "B", title: "Byes", count: tiers.filter((tier) => tier === "bye").length, live: racer.liveTier === "bye", liveAlreadyCounted: true},
    {label: "P", title: "Preliminary", count: tiers.filter((tier) => tier === "preliminary").length, live: racer.liveTier === "preliminary", liveAlreadyCounted: true},
    {label: "WC", title: "Wildcard", count: tiers.filter((tier) => tier === "wildcard").length, live: racer.liveTier === "wildcard", liveAlreadyCounted: true},
  ];
  // Wins exclude the live round in the API and need a provisional +1. Live
  // championship tiers are already present in dayChampionshipTiers, so their
  // count only needs the asterisk and must not be incremented again.
  return stats
    .map((stat) => ({...stat, displayCount: stat.live && !stat.liveAlreadyCounted ? stat.count + 1 : stat.count}))
    .filter((stat) => stat.displayCount > 0);
}

function statBadgesMarkup(racer, wrapperClass, chipClass) {
  return `<div class="${wrapperClass}">${racerStatBadges(racer).map((stat) => `<span class="${chipClass} stat-${stat.label.toLowerCase()}" title="${stat.title}${stat.live ? " · leading, not yet finalized" : ""}">${stat.displayCount}${stat.live ? "*" : ""}<small>${stat.label}</small></span>`).join("")}</div>`;
}

function kioskStatBadgesMarkup(racer) {
  return statBadgesMarkup(racer, "kiosk-stat-badges", "kiosk-stat-chip");
}

function standingStatBadgesMarkup(racer) {
  return statBadgesMarkup(racer, "standing-stat-badges", "standing-stat-chip");
}

function renderKioskDashboard() {
  const c = state.competition;
  const championship = state.championship;
  if (championship.final.ready) return renderKioskFinalDashboard();
  const status = currentTournamentStatus();
  const nextHeat = state.days.flatMap((day) => day.heats).find((heat) => !heat.complete);
  const nextWildcard = championship.wildcard.heats.find((heat) => !heat.complete);
  const nextPreliminary = championship.preliminary.heats.find((heat) => !heat.complete);
  const leader = state.standings[0];
  const visibleStandings = state.standings.slice(0, 8);
  const completedRounds = state.days.filter((day) => day.heats.every((heat) => heat.complete)).length;
  const progress = progressPercent();
  const liveStatus = kioskRefreshFailed ? "Reconnecting…" : kioskTimestamp();
  let nextContent = "";

  if (nextHeat) {
    nextContent = `<div class="kiosk-card-heading"><p class="kiosk-card-label">${nextHeat.started ? "In progress" : "Up next"}</p><b>Race #${nextHeat.globalNumber}</b></div><h2>Round ${nextHeat.day} · Heat ${nextHeat.heatNumber}</h2><div class="kiosk-next-racers">${nextHeat.entries.map((entry) => `<span>${marble(entry.color, "small")}<b>${escapeHtml(entry.name)}</b></span>`).join("")}</div>`;
  } else if (nextWildcard) {
    nextContent = `<div class="kiosk-card-heading"><p class="kiosk-card-label">${nextWildcard.started ? "In progress" : "Up next"}</p><b>Championship</b></div><h2>Championship: Wildcard Heat ${nextWildcard.heatNumber}</h2><div class="kiosk-next-racers">${nextWildcard.entries.map((entry) => `<span>${marble(entry.color, "small")}<b>${escapeHtml(entry.name)}</b></span>`).join("")}</div>`;
  } else if (nextPreliminary) {
    nextContent = `<div class="kiosk-card-heading"><p class="kiosk-card-label">${nextPreliminary.started ? "In progress" : "Up next"}</p><b>Championship</b></div><h2>Championship: Preliminary Heat ${nextPreliminary.heatNumber}</h2><div class="kiosk-next-racers">${nextPreliminary.entries.map((entry) => `<span>${marble(entry.color, "small")}<b>${escapeHtml(entry.name)}</b></span>`).join("")}</div>`;
  } else {
    nextContent = `<p class="kiosk-card-label">Up next</p><div class="kiosk-final-ready"><span>★</span><div><strong>Championship in progress</strong><p>Check the bracket for the current stage.</p></div></div>`;
  }

  return `<section class="kiosk-dashboard" aria-label="Live tournament dashboard">
    <header class="kiosk-header">
      <a class="kiosk-brand" href="/" aria-label="RollRank home"><img src="/static/marble-logo.png" alt="" width="48" height="48"><span><strong>RollRank</strong><small>Live tournament display</small></span></a>
      <div class="kiosk-title"><p>${status.label}</p><h1>${escapeHtml(c.name)}</h1></div>
      <div class="kiosk-controls"><span class="kiosk-live-status${kioskRefreshFailed ? " stale" : ""}"><i></i>${liveStatus}</span><button type="button" data-exit-kiosk aria-label="Exit fullscreen display">Exit <span aria-hidden="true">×</span></button></div>
    </header>
    <div class="kiosk-overview">
      <article class="kiosk-leader-card">
        <p class="kiosk-card-label">Live leader</p>
        <div class="kiosk-leader-racer">${marble(leader.color)}<div><strong>${escapeHtml(leader.name)}</strong><span>Round leader</span></div><b>${leader.wins}<small> win${leader.wins === 1 ? "" : "s"}</small></b></div>
      </article>
      <article class="kiosk-progress-card">
        <div class="kiosk-card-heading"><p class="kiosk-card-label">Heat progress</p><b>${c.completedHeats}/${c.totalHeats}</b></div>
        <div class="kiosk-progress-value"><strong>${progress}%</strong><span>${completedRounds} of ${c.days} rounds complete</span></div>
        <div class="kiosk-progress-track"><i style="width:${progress}%"></i></div>
      </article>
      <article class="kiosk-next-card">${nextContent}</article>
    </div>
    <section class="kiosk-standings-panel">
      <header><div><p class="kiosk-card-label">Current standings</p><h2>Round-by-round leaderboard</h2></div></header>
      <div class="kiosk-standing-list">
        ${visibleStandings.map((racer) => `<article><span class="kiosk-rank">${racer.rank}</span>${marble(racer.color, "small")}<strong>${escapeHtml(racer.name)}</strong>${kioskStatBadgesMarkup(racer)}</article>`).join("")}
      </div>
      <footer><span>Showing ${visibleStandings.length} of ${state.standings.length} racers</span></footer>
    </section>
  </section>`;
}

function resultOptions(count, selected) {
  let result = `<option value="">Place</option>`;
  for (let position = 1; position <= count; position += 1) {
    result += `<option value="${position}" ${Number(selected) === position ? "selected" : ""}>${ordinal(position)}</option>`;
  }
  result += `<option value="0" ${selected != null && Number(selected) === 0 ? "selected" : ""}>DNF · 0 pts</option>`;
  return result;
}

function finalDnfPlace(racers) {
  return racers.filter((racer) => Number(racer.finish) > 0).length + 1;
}

function finalFinishLabel(finish, racers) {
  return Number(finish) === 0 ? `Tied ${ordinal(finalDnfPlace(racers))} · DNF` : `${ordinal(finish)} place`;
}

function marblePlaceLabel(raceMarble) {
  return raceMarble.finish == null ? "-" : raceMarble.finish === 0 ? "DNF" : ordinal(raceMarble.finish);
}

function entryPlaceSummary(entry) {
  const labels = entry.marbles.map(marblePlaceLabel);
  return labels.every((label) => label === "-") ? "-" : labels.join(", ");
}

function heatRacerPreview(heat) {
  return `<div class="racer-preview">${heat.entries.map((entry) => `<span>${marble(entry.color, "small")}<b>${escapeHtml(entry.name)}</b></span>`).join("")}</div>`;
}

function lockedHeatCard(heat) {
  return `<article class="heat-card locked" id="heat-${heat.id}">
    <header class="heat-card-heading">
      <div class="heat-title"><span>Heat</span><strong>${heat.heatNumber}</strong><small>Race #${heat.globalNumber}</small></div>
      <div><span class="pending-chip">Locked</span></div>
      <span class="heat-locked-note">Complete the earlier rounds first</span>
    </header>
    ${heatRacerPreview(heat)}
  </article>`;
}

function readyToStartHeatCard(heat) {
  return `<article class="heat-card ready" id="heat-${heat.id}">
    <header class="heat-card-heading">
      <div class="heat-title"><span>Heat</span><strong>${heat.heatNumber}</strong><small>Race #${heat.globalNumber}</small></div>
      <div><span class="pending-chip">Ready to start</span></div>
      <button class="primary-button compact" data-start-heat="${heat.id}">Start heat</button>
    </header>
    ${heatRacerPreview(heat)}
  </article>`;
}

function editLockedHeatCard(heat) {
  return `<article class="heat-card complete locked" id="heat-${heat.id}">
    <header class="heat-card-heading">
      <div class="heat-title"><span>Heat</span><strong>${heat.heatNumber}</strong><small>Race #${heat.globalNumber}</small></div>
      <div><span class="complete-chip">Complete</span></div>
      <span class="heat-locked-note">Locked · a later heat has started</span>
    </header>
    <div class="heat-entry-list">
      ${heat.entries.map((entry) => `<div class="heat-entry">
        <span class="lane">${entry.lane}</span>${marble(entry.color, "small")}<span class="heat-entry-name"><strong>${escapeHtml(entry.name)}</strong>${originBadge(entry)}</span>
        <div class="marble-results">${entry.marbles.map((raceMarble) => `<div class="marble-result"><span>M${raceMarble.number}<small>${heat.stage === "staging" ? (raceMarble.points == null ? "-" : `${raceMarble.points} pts`) : marblePlaceLabel(raceMarble)}</small></span><b>${raceMarble.finish == null ? "-" : raceMarble.finish === 0 ? "DNF" : ordinal(raceMarble.finish)}</b></div>`).join("")}</div>
        <span class="points-result">${heat.stage === "staging" ? (entry.points == null ? "-" : `${entry.points} total`) : entryPlaceSummary(entry)}</span>
      </div>`).join("")}
    </div>
  </article>`;
}

function heatEditor(heat) {
  if (heat.locked) return lockedHeatCard(heat);
  if (heat.started === false) return readyToStartHeatCard(heat);
  if (heat.editLocked) return editLockedHeatCard(heat);
  const finishCount = heat.entries.reduce((total, entry) => total + entry.marbles.length, 0);
  return `<article class="heat-card ${heat.complete ? "complete" : ""}" id="heat-${heat.id}">
    <header class="heat-card-heading">
      <div class="heat-title"><span>Heat</span><strong>${heat.heatNumber}</strong><small>Race #${heat.globalNumber}</small></div>
      <div><span class="${heat.complete ? "complete-chip" : "pending-chip"}">${heat.complete ? "Complete" : "Awaiting results"}</span></div>
      <button class="save-heat-button" data-save-heat="${heat.id}">${heat.complete ? "Update results" : "Save results"}</button>
    </header>
    <div class="heat-entry-list">
      ${heat.entries.map((entry) => `<div class="heat-entry">
        <span class="lane">${entry.lane}</span>${marble(entry.color, "small")}<span class="heat-entry-name"><strong>${escapeHtml(entry.name)}</strong>${originBadge(entry)}</span>
        <div class="marble-results">${entry.marbles.map((raceMarble) => `<label class="marble-result"><span>M${raceMarble.number}<small>${heat.stage === "staging" ? (raceMarble.points == null ? "-" : `${raceMarble.points} pts`) : marblePlaceLabel(raceMarble)}</small></span><select aria-label="Finish for ${escapeHtml(entry.name)}, marble ${raceMarble.number}" data-result-for="${entry.contestantId}" data-marble-number="${raceMarble.number}">${resultOptions(finishCount, raceMarble.finish)}</select></label>`).join("")}</div>
        <span class="points-result">${heat.stage === "staging" ? (entry.points == null ? "-" : `${entry.points} total`) : entryPlaceSummary(entry)}</span>
      </div>`).join("")}
    </div>
  </article>`;
}

function championshipTabStatus() {
  const champ = state.championship;
  if (champ.final.complete) return "Complete";
  if (champ.wildcard.ready) return "In progress";
  return "Locked";
}

function renderHeats() {
  const onChampionship = activeDay === "championship";
  const selectedDay = onChampionship ? null : state.days.find((day) => day.day === activeDay) || state.days[0];
  return `${viewHeader("Results desk", "Rounds", "Choose a round, then record a unique finishing position for every marble.")}
    <div class="day-tabs" role="tablist">${state.days.map((day) => {
      const complete = day.heats.filter((heat) => heat.complete).length;
      return `<button role="tab" aria-selected="${day.day === activeDay}" class="${day.day === activeDay ? "active" : ""}" data-day="${day.day}"><span>Round ${day.day}</span><small>${complete}/${day.heats.length} complete</small></button>`;
    }).join("")}<button role="tab" aria-selected="${onChampionship}" class="${onChampionship ? "active" : ""}" data-day="championship"><span>Championship</span><small>${championshipTabStatus()}</small></button></div>
    ${onChampionship ? renderChampionshipStages() : `<section class="heat-list">${selectedDay.heats.map(heatEditor).join("")}</section>`}`;
}

function ladderLockedPlaceholder(label) {
  return `<div class="ladder-placeholder"><span aria-hidden="true">🔒</span><p>${label}</p></div>`;
}

function seedRoundsTag(seedRounds) {
  if (!seedRounds || !seedRounds.length) return "";
  const label = seedRounds.length === 1 ? `Round ${seedRounds[0]}` : `Rounds ${seedRounds.join(", ")}`;
  return `<small class="seed-tag">${label}</small>`;
}

function seedHeatTag(entry, sourceHeats, stageLabel) {
  if (!sourceHeats || !sourceHeats.length) return "";
  const heatIds = new Set((entry.marbles || []).map((raceMarble) => raceMarble.originHeatId).filter((id) => id != null));
  if (!heatIds.size) return "";
  const heatNumbers = sourceHeats
    .filter((heat) => heatIds.has(heat.id))
    .map((heat) => heat.heatNumber)
    .sort((a, b) => a - b);
  if (!heatNumbers.length) return "";
  const label = heatNumbers.length === 1
    ? `${stageLabel} Heat ${heatNumbers[0]}`
    : `${stageLabel} Heats ${heatNumbers.join(", ")}`;
  return `<small class="seed-tag">${escapeHtml(label)}</small>`;
}

function ladderEntryLabel(name, marbleCount, seedRounds, extraTag = "") {
  // A direct wildcard/preliminary heat seed is a more specific answer to
  // "where did this racer come from" than the original staging round(s)
  // behind that heat win, so it takes over the tag slot entirely.
  return `<div class="ladder-entry-info"><span>${escapeHtml(name)}${marbleCount > 1 ? ` <span class="marble-count">×${marbleCount}</span>` : ""}</span>${extraTag || seedRoundsTag(seedRounds)}</div>`;
}

function ladderProjectedRoster(entries, lockedLabel) {
  return `<div class="ladder-heat projected">
    <div class="ladder-heat-head"><span aria-hidden="true">🔒</span><span>Not yet locked in</span></div>
    <ul class="ladder-entries">
      ${entries.map((entry) => entry.decided
        ? `<li class="ladder-entry ${tierClass(entry.originStage === "wildcard" ? "wildcard" : entry.originStage === "bye" ? "bye" : "preliminary")}">${marble(entry.color, "small")}${ladderEntryLabel(entry.name, entry.marbleSlots || 1, entry.seedRounds)}</li>`
        : `<li class="ladder-entry pending"><span class="tbd-marble" aria-hidden="true">?</span><span>Heat ${entry.heatNumber}${entry.qualifyingPlace ? ` · ${ordinal(entry.qualifyingPlace)} racer` : " winner"}</span><b>TBD</b></li>`
      ).join("")}
    </ul>
    <p class="ladder-projected-note">${lockedLabel}</p>
  </div>`;
}

function ladderSkippedPlaceholder(stage) {
  const note = stage.fieldSize
    ? `${stage.fieldSize} racer${stage.fieldSize === 1 ? "" : "s"} advanced automatically.`
    : "Nobody qualified for this stage.";
  return `<div class="ladder-placeholder skip"><span aria-hidden="true">→</span><p>${note}</p></div>`;
}

function ladderPlaceLabel(entry) {
  const label = entryPlaceSummary(entry);
  return label === "-" ? "" : label;
}

function ladderHeatCard(heat, sourceHeats, sourceStageLabel) {
  return `<article class="ladder-heat ${heat.complete ? "complete" : ""}">
    <div class="ladder-heat-head"><span>Heat ${heat.heatNumber}</span><span class="${heat.complete ? "complete-chip" : "pending-chip"}">${heat.complete ? "Complete" : "In progress"}</span></div>
    <ul class="ladder-entries">
      ${heat.entries.map((entry) => `<li class="ladder-entry${entry.finish === 0 ? " dnf" : ""}">
        ${marble(entry.color, "small")}${ladderEntryLabel(entry.name, entry.marbles.length, entry.seedRounds, seedHeatTag(entry, sourceHeats, sourceStageLabel))}<b>${ladderPlaceLabel(entry)}</b>
      </li>`).join("")}
    </ul>
  </article>`;
}

function ladderStageColumn(stage, lockedLabel, sourceHeats, sourceStageLabel) {
  if (!stage.ready) {
    if (stage.projectedEntries && stage.projectedEntries.length) return ladderProjectedRoster(stage.projectedEntries, lockedLabel);
    return ladderLockedPlaceholder(lockedLabel);
  }
  if (stage.skipped) return ladderSkippedPlaceholder(stage);
  return `<div class="ladder-heats">${stage.heats.map((heat) => ladderHeatCard(heat, sourceHeats, sourceStageLabel)).join("")}</div>`;
}

function ladderFinalColumn(finalStage, sourceHeats) {
  if (!finalStage.ready) {
    if (finalStage.projectedEntries && finalStage.projectedEntries.length) {
      return ladderProjectedRoster(finalStage.projectedEntries, "Preliminary results decide the final field.");
    }
    return ladderLockedPlaceholder("Preliminary results decide the final field.");
  }
  if (!finalStage.heat) return `<div class="ladder-placeholder"><span aria-hidden="true">—</span><p>No final field in this tournament.</p></div>`;
  const entries = finalStage.heat.entries;
  return `<article class="ladder-heat final ${finalStage.complete ? "complete" : ""}">
    <div class="ladder-heat-head"><span>The Final</span><span class="${finalStage.complete ? "complete-chip" : "pending-chip"}">${finalStage.complete ? "Complete" : "In progress"}</span></div>
    <ul class="ladder-entries">
      ${entries.map((entry) => `<li class="ladder-entry${entry.finish === 0 ? " dnf" : ""}">
        ${marble(entry.color, "small")}${ladderEntryLabel(entry.name, 1, entry.seedRounds, seedHeatTag(entry, sourceHeats, "Preliminary"))}<b>${entry.finish === 1 ? "🏆" : ladderPlaceLabel(entry)}</b>
      </li>`).join("")}
    </ul>
  </article>`;
}

function renderChampionshipLadder() {
  const champ = state.championship;
  return `<section class="panel ladder-panel">
    <div class="panel-heading"><div><p class="eyebrow">Championship bracket</p><h2>Championship ladder</h2></div></div>
    <div class="ladder">
      <div class="ladder-column">
        <div class="ladder-column-heading"><span>Stage 1</span><h3>Wildcard</h3></div>
        ${ladderStageColumn(champ.wildcard, "Runs once every round heat is scored.")}
      </div>
      <div class="ladder-connector" aria-hidden="true">→</div>
      <div class="ladder-column">
        <div class="ladder-column-heading"><span>Stage 2</span><h3>Preliminary</h3></div>
        ${ladderStageColumn(champ.preliminary, "Runs once every wildcard heat is scored.", champ.wildcard.heats, "Wildcard")}
      </div>
      <div class="ladder-connector" aria-hidden="true">→</div>
      <div class="ladder-column">
        <div class="ladder-column-heading"><span>Stage 3</span><h3>Final</h3></div>
        ${ladderFinalColumn(champ.final, champ.preliminary.heats)}
      </div>
    </div>
  </section>`;
}

function renderStandings() {
  const c = state.competition;
  return `${viewHeader("Live scoring", "Tournament standings", "Round placings update automatically whenever a heat result is saved.")}
    ${renderChampionshipLadder()}
    <section class="panel table-panel"><div class="table-scroll"><table class="standings-table"><thead><tr><th>Racer</th>${Array.from({length:c.days}, (_, i) => `<th>Round ${i + 1}</th>`).join("")}</tr></thead><tbody>
    ${state.standings.map((racer) => `<tr><td><span class="racer-cell">${marble(racer.color, "small")}<strong>${escapeHtml(racer.name)}</strong></span></td>${racer.dayPlacements.map((place, index) => `<td class="${tierClass(racer.dayChampionshipTiers[index])}">${placeLabel(place, index + 1 === c.liveRoundDay)}${racer.dayChampionshipTiers[index] ? `<small class="tier-tag">${tierLabel[racer.dayChampionshipTiers[index]]}</small>` : ""}</td>`).join("")}</tr>`).join("")}
    </tbody></table></div>
    <div class="mobile-standings" aria-label="Mobile standings">
      ${state.standings.map((racer) => `<article class="mobile-standing-card">
        <div class="mobile-standing-lead">${marble(racer.color, "small")}<strong>${escapeHtml(racer.name)}</strong></div>
        <div class="mobile-standing-stats">${racer.dayPlacements.map((place, index) => `<span class="${tierClass(racer.dayChampionshipTiers[index])}"><small>Round ${index + 1}</small><b>${placeLabel(place, index + 1 === c.liveRoundDay)}</b>${racer.dayChampionshipTiers[index] ? `<small class="tier-tag">${tierLabel[racer.dayChampionshipTiers[index]]}</small>` : ""}</span>`).join("")}</div>
      </article>`).join("")}
    </div></section>`;
}

function renderFinalCelebration(finalStage) {
  const entries = finalStage.heat.entries;
  const finishers = entries.filter((entry) => entry.finish > 0).sort((first, second) => first.finish - second.finish);
  const dnfs = entries.filter((entry) => entry.finish === 0);
  const podiumMeta = {
    1: {className:"gold", label:"Gold trophy"},
    2: {className:"silver", label:"Silver trophy"},
    3: {className:"bronze", label:"Bronze trophy"},
  };
  const podium = finishers.filter((entry) => entry.finish <= 3);
  const remaining = [...finishers.filter((entry) => entry.finish > 3), ...dnfs];
  return `<section class="final-celebration" aria-labelledby="final-results-title">
    <div class="final-results-heading"><div><p class="eyebrow">Final results</p><h2 id="final-results-title">${podium.length === 3 ? "Top 3 podium" : "Final podium"}</h2></div><p>${escapeHtml(finalStage.champion.name)} takes the title.</p></div>
    <div class="podium count-${podium.length}" aria-label="Final podium">
      ${podium.map((entry) => {
        const meta = podiumMeta[entry.finish];
        return `<article class="podium-place ${meta.className}">
          <div class="podium-racer"><span class="podium-trophy" role="img" aria-label="${meta.label}">🏆</span>${marble(entry.color)}<strong>${escapeHtml(entry.name)}</strong><small>${ordinal(entry.finish)} place</small></div>
          <div class="podium-step"><b>${entry.finish}</b><span>${meta.className}</span></div>
        </article>`;
      }).join("")}
    </div>
    ${remaining.length ? `<div class="non-podium-list"><div class="non-podium-heading"><strong>Remaining finalists</strong><span>${dnfs.length ? `${dnfs.length} DNF${dnfs.length === 1 ? "" : "s"} tied for ${ordinal(finalDnfPlace(entries))}` : "Final order"}</span></div>${remaining.map((entry) => `<article class="${entry.finish === 0 ? "dnf" : ""}"><span class="sad-face" role="img" aria-label="Sad face">😢</span>${marble(entry.color, "small")}<strong>${escapeHtml(entry.name)}</strong><span>${finalFinishLabel(entry.finish, entries)}</span></article>`).join("")}</div>` : ""}
  </section>`;
}

function championshipStatusCard(icon, eyebrow, title, description, extraClass = "") {
  return `<section class="championship-lock panel ${extraClass}"><div class="lock-icon">${icon}</div><div><p class="eyebrow">${eyebrow}</p><h2>${title}</h2><p>${description}</p></div></section>`;
}

function renderChampionshipStageBody(stage, lockedDescription) {
  if (!stage.ready) return championshipStatusCard("⏳", "Not ready", "Waiting on the previous stage", lockedDescription);
  if (stage.skipped) {
    const note = stage.fieldSize
      ? `${stage.fieldSize} racer${stage.fieldSize === 1 ? "" : "s"} advanced automatically — the field was too small for a heat.`
      : "Nobody qualified for this stage in this tournament.";
    return championshipStatusCard("→", "No heats needed", "Advanced automatically", note, "skipped");
  }
  return `<section class="heat-list">${stage.heats.map(heatEditor).join("")}</section>`;
}

function renderFinalStageBody(finalStage) {
  if (!finalStage.ready) {
    const promoted = state.competition.preliminaryRacersPromotedPerHeat;
    return championshipStatusCard("⏳", "Not ready", "Waiting on the preliminary round", `The top ${promoted} racer${promoted === 1 ? "" : "s"} from each preliminary heat and round-win byes race here once every preliminary heat is scored.`);
  }
  if (!finalStage.heat) {
    return championshipStatusCard("→", "No final field", "Nobody qualified", "This tournament didn't produce any finalists.");
  }
  if (finalStage.complete) return renderFinalCelebration(finalStage);
  return `<section class="heat-list">${heatEditor(finalStage.heat)}</section>`;
}

function championshipStageChip(stage) {
  if (!stage.ready) return `<span class="pending-chip">Locked</span>`;
  return `<span class="${stage.complete ? "complete-chip" : "pending-chip"}">${stage.complete ? "Complete" : "In progress"}</span>`;
}

function championshipReportLink() {
  return `<a class="print-button compact" href="${reportUrl()}" target="_blank" rel="noopener"><span class="pdf-icon" aria-hidden="true"></span> Printable report</a>`;
}

function renderChampionshipStages() {
  const champ = state.championship;
  const c = state.competition;
  const wildcardPromoted = c.wildcardRacersPromotedPerHeat;
  return `<section class="championship-stage">
      <div class="panel-heading"><div><p class="eyebrow">Stage 1</p><h2>Wildcard heats</h2></div>${championshipStageChip(champ.wildcard)}</div>
      ${renderChampionshipStageBody(champ.wildcard, `3rd and 4th place finishers from every round race here once all ${c.totalHeats} round heats are complete.`)}
    </section>
    <section class="championship-stage">
      <div class="panel-heading"><div><p class="eyebrow">Stage 2</p><h2>Preliminary heats</h2></div>${championshipStageChip(champ.preliminary)}</div>
      ${renderChampionshipStageBody(champ.preliminary, `The top ${wildcardPromoted} racer${wildcardPromoted === 1 ? "" : "s"} from each wildcard heat and the round runners-up race here once every wildcard heat is scored.`)}
    </section>
    <section class="championship-stage">
      <div class="panel-heading"><div><p class="eyebrow">Stage 3</p><h2>${champ.final.complete ? "Final results" : "The final"}</h2></div><div class="panel-heading-actions">${championshipStageChip(champ.final)}${champ.final.complete ? championshipReportLink() : ""}</div></div>
      ${renderFinalStageBody(champ.final)}
    </section>`;
}

function contestantRow(contestant = {name:"", color:"#2F80ED"}) {
  return `<div class="contestant-config-row"><span class="drag-handle" aria-hidden="true">⋮⋮</span><input type="color" value="${escapeHtml(contestant.color)}" aria-label="Marble color"><input type="text" value="${escapeHtml(contestant.name)}" maxlength="50" aria-label="Racer name" required><button type="button" data-remove-contestant aria-label="Remove racer">×</button></div>`;
}

function renderSetup() {
  const c = state.competition;
  return `${viewHeader("Tournament settings", "Configure this tournament", "Every tournament keeps its own format, racers, heat results, standings, and final.")}
    <form id="config-form" class="setup-grid">
      <section class="panel config-panel"><div class="section-title"><span>01</span><div><h2>Tournament format</h2><p>Name the event and define its schedule.</p></div></div>
        <label class="field wide"><span>Tournament name</span><input name="name" value="${escapeHtml(c.name)}" maxlength="80" required></label>
        <div class="config-group">
          <h3 class="eyebrow">Staging rounds</h3>
          <div class="field-grid"><label class="field"><span>Race rounds</span><input name="days" type="number" min="1" max="30" value="${c.days}" required></label><label class="field"><span>Heats per racer / round</span><input name="heatsPerRacerPerDay" type="number" min="1" max="20" value="${c.heatsPerRacerPerDay}" required></label><label class="field"><span>Max marbles per heat</span><input name="maxMarblesPerHeat" type="number" min="2" max="480" value="${c.maxMarblesPerHeat}" required><small>The app automatically chooses the largest full heat under this limit.</small></label><label class="field"><span>Marbles per racer / heat</span><input name="marblesPerRacer" type="number" min="1" max="20" value="${c.marblesPerRacer}" required><small>Applies to round heats only.</small></label></div>
          <div class="schedule-preview" id="schedule-preview"><strong>${c.heatsPerDay} heats per round · ${c.racersPerHeat} racers per heat</strong><span>Every racer appears ${c.heatsPerRacerPerDay} times each round; each heat uses ${c.marblesPerHeat} of the ${c.maxMarblesPerHeat} allowed marbles.</span></div>
          <label class="field wide"><span>Points by finishing place</span><input name="points" value="${state.points.join(", ")}" required><small>Comma-separated, starting with first place. Missing places receive zero points.</small></label>
        </div>
        <div class="config-group">
          <h3 class="eyebrow">Championship round</h3>
          <div class="field-grid">
            <label class="field"><span>Max marbles in wildcard/prelim heats</span><input name="championshipMaxMarblesPerHeat" type="number" min="2" max="480" value="${c.championshipMaxMarblesPerHeat}" required><small>Sizes wildcard and preliminary heats automatically.</small></label>
            <label class="field"><span>Max bye marbles per racer</span><input name="maxByeMarblesPerRacer" type="number" min="0" max="20" value="${c.maxByeMarblesPerRacer}" required><small>Caps how many round wins one racer can bank as byes, and how many marbles a racer can earn in the preliminary heat. Wildcard marbles are uncapped.</small></label>
            <label class="field"><span>Wildcard racers promoted / heat</span><input name="wildcardRacersPromotedPerHeat" type="number" min="1" max="24" value="${c.wildcardRacersPromotedPerHeat}" required><small>Top racers from each wildcard heat who advance to the preliminary stage.</small></label>
            <label class="field"><span>Preliminary racers promoted / heat</span><input name="preliminaryRacersPromotedPerHeat" type="number" min="1" max="24" value="${c.preliminaryRacersPromotedPerHeat}" required><small>Top racers from each preliminary heat who advance to the final.</small></label>
            <label class="field"><span>Max Racers in Final</span><input name="maxFinalRacers" type="number" min="2" max="24" value="${c.maxFinalRacers}" required><small>Trims the final field if byes and preliminary qualifiers exceed this.</small></label>
          </div>
        </div>
      </section>
      <section class="panel config-panel"><div class="section-title"><span>02</span><div><h2>Racers</h2><p>Names and colors are used throughout the race sheets.</p></div></div><div id="contestant-list">${state.contestants.map(contestantRow).join("")}</div><button type="button" class="secondary-button full" data-add-contestant>+ Add racer</button></section>
      <section class="panel tournament-management"><div><p class="eyebrow">Tournament library</p><h2>Manage this tournament</h2><p>Create another tournament from the selector above, or permanently remove this one.</p></div><button type="button" class="danger-button" data-delete-tournament>Delete tournament</button></section>
      <div class="save-bar"><div><strong>Ready to update?</strong><span>Only ${escapeHtml(c.name)} is changed; other tournaments stay untouched.</span></div><button type="submit" class="primary-button">Save tournament</button></div>
    </form>`;
}

function render() {
  if (!state) return;
  document.querySelectorAll("[data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === activeView));
  const views = {dashboard:kioskMode ? renderKioskDashboard : renderDashboard, heats:renderHeats, standings:renderStandings, setup:renderSetup};
  app.innerHTML = (views[activeView] || renderDashboard)();
  if (activeView === "setup") requestAnimationFrame(updateSchedulePreview);
  if (!kioskMode) app.focus({preventScroll:true});
  if (activeViewIsLive()) startLiveUpdates(); else stopLiveUpdates();
}

async function startHeat(heatId) {
  try {
    applyState(await api(`/api/heats/${heatId}/start`, {method:"PUT"}));
    notify("Heat started.");
  } catch (error) { notify(error.message, true); }
}

async function saveHeat(heatId) {
  const card = document.querySelector(`#heat-${heatId}`);
  const results = [...card.querySelectorAll("[data-result-for]")].map((select) => ({contestantId:Number(select.dataset.resultFor), marbleNumber:Number(select.dataset.marbleNumber), finish:select.value}));
  if (results.some((result) => !result.finish)) return notify("Choose a finishing position for every marble.", true);
  try {
    applyState(await api(`/api/heats/${heatId}/results`, {method:"PUT", body:JSON.stringify({results})}));
    notify("Heat results saved.");
  } catch (error) { notify(error.message, true); }
}

function configPayload(confirmReset = false) {
  const form = document.querySelector("#config-form");
  const formData = new FormData(form);
  const contestants = [...form.querySelectorAll(".contestant-config-row")].map((row) => ({color:row.querySelector('input[type="color"]').value, name:row.querySelector('input[type="text"]').value}));
  return {name:formData.get("name"), days:formData.get("days"), heatsPerRacerPerDay:formData.get("heatsPerRacerPerDay"), maxMarblesPerHeat:formData.get("maxMarblesPerHeat"), marblesPerRacer:formData.get("marblesPerRacer"), championshipMaxMarblesPerHeat:formData.get("championshipMaxMarblesPerHeat"), maxByeMarblesPerRacer:formData.get("maxByeMarblesPerRacer"), wildcardRacersPromotedPerHeat:formData.get("wildcardRacersPromotedPerHeat"), preliminaryRacersPromotedPerHeat:formData.get("preliminaryRacersPromotedPerHeat"), maxFinalRacers:formData.get("maxFinalRacers"), points:String(formData.get("points")).split(",").map((value) => value.trim()), contestants, confirmReset};
}

function updateSchedulePreview() {
  const form = document.querySelector("#config-form");
  const preview = document.querySelector("#schedule-preview");
  if (!form || !preview) return;
  const racerCount = form.querySelectorAll(".contestant-config-row").length;
  const maxMarblesPerHeat = Number(form.elements.maxMarblesPerHeat.value);
  const appearances = Number(form.elements.heatsPerRacerPerDay.value);
  const marblesPerRacer = Number(form.elements.marblesPerRacer.value);
  const days = Number(form.elements.days.value);
  const slots = racerCount * appearances;
  const racerCapacity = Math.min(racerCount, 24, Math.floor(maxMarblesPerHeat / marblesPerRacer));
  let raceSize = 0;
  for (let candidate = racerCapacity; candidate >= 2; candidate -= 1) {
    if (slots % candidate === 0) { raceSize = candidate; break; }
  }
  if (!appearances || !marblesPerRacer || !maxMarblesPerHeat || !raceSize) {
    preview.classList.add("invalid");
    preview.innerHTML = `<strong>No full heat schedule fits this maximum</strong><span>Increase max marbles per heat or adjust heats per racer per round.</span>`;
    return;
  }
  const heatsPerDay = slots / raceSize;
  preview.classList.remove("invalid");
  const marblesPerHeat = raceSize * marblesPerRacer;
  preview.innerHTML = `<strong>${heatsPerDay} heats per round · ${heatsPerDay * days} total</strong><span>${raceSize} racers per heat with ${marblesPerRacer} marble${marblesPerRacer === 1 ? "" : "s"} each; ${marblesPerHeat} of ${maxMarblesPerHeat} allowed marbles are used.</span>`;
}

async function saveConfiguration(event) {
  event.preventDefault();
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return notify("The settings form could not be submitted.", true);
  const submit = form.querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    applyState(await api(`/api/tournaments/${activeTournamentId}`, {method:"PUT", body:JSON.stringify(configPayload(false))}));
    notify("Tournament settings saved.");
  } catch (error) {
    if (error.requiresReset && window.confirm(`${error.message}\n\nContinue and rebuild the schedule?`)) {
      try { applyState(await api(`/api/tournaments/${activeTournamentId}`, {method:"PUT", body:JSON.stringify(configPayload(true))})); notify("This tournament's schedule was rebuilt."); }
      catch (secondError) { notify(secondError.message, true); }
    } else if (!error.requiresReset) notify(error.message, true);
  } finally { submit.disabled = false; }
}

async function createTournament(event) {
  event.preventDefault();
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return notify("The tournament form could not be submitted.", true);
  const submit = form.querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    const formData = new FormData(form);
    const created = await api("/api/tournaments", {method:"POST", body:JSON.stringify({name:formData.get("name")})});
    const tournamentId = created.competition.id;
    tournamentDialog.close();
    form.reset();
    if (window.location.pathname !== "/workspace") {
      window.location.assign(`/workspace?tournament=${tournamentId}&view=setup`);
      return;
    }
    activeDay = 1;
    activeView = "setup";
    applyState(created);
    notify("New tournament created. Its settings and results are independent.");
  } catch (error) { notify(error.message, true); }
  finally { submit.disabled = false; }
}

async function deleteCurrentTournament() {
  if (!window.confirm(`Delete ${state.competition.name}?\n\nIts racers, heats, results, final, and report data will be permanently removed.`)) return;
  try {
    const result = await api(`/api/tournaments/${activeTournamentId}`, {method:"DELETE"});
    if (!result.nextTournamentId) {
      window.location.assign("/");
      return;
    }
    activeDay = 1;
    activeView = "dashboard";
    await loadState(result.nextTournamentId);
    notify("Tournament deleted.");
  } catch (error) { notify(error.message, true); }
}

document.addEventListener("click", (event) => {
  if (event.target.closest("[data-enter-kiosk]")) { setKioskMode(true, true); return; }
  if (event.target.closest("[data-exit-kiosk]")) { setKioskMode(false); return; }
  const viewButton = event.target.closest("[data-view]");
  if (viewButton) {
    activeView = viewButton.dataset.view;
    if (viewButton.dataset.day) activeDay = viewButton.dataset.day === "championship" ? "championship" : Number(viewButton.dataset.day);
    render();
    window.scrollTo({top:0, behavior:"smooth"});
    return;
  }
  if (event.target.closest("[data-new-tournament]")) { tournamentDialog.showModal(); requestAnimationFrame(() => tournamentDialog.querySelector('input[name="name"]').focus()); return; }
  if (event.target.closest("[data-close-tournament-dialog]")) { tournamentDialog.close(); return; }
  if (event.target.closest("[data-delete-tournament]")) { deleteCurrentTournament(); return; }
  const dayButton = event.target.closest("[data-day]");
  if (dayButton) { activeDay = dayButton.dataset.day === "championship" ? "championship" : Number(dayButton.dataset.day); render(); return; }
  const scoreButton = event.target.closest("[data-score-heat]");
  if (scoreButton) { const heat = state.days.flatMap((day) => day.heats).find((item) => item.id === Number(scoreButton.dataset.scoreHeat)); activeDay = heat.day; activeView = "heats"; render(); requestAnimationFrame(() => document.querySelector(`#heat-${heat.id}`)?.scrollIntoView({behavior:"smooth", block:"center"})); return; }
  const saveHeatButton = event.target.closest("[data-save-heat]");
  if (saveHeatButton) { saveHeat(Number(saveHeatButton.dataset.saveHeat)); return; }
  const startHeatButton = event.target.closest("[data-start-heat]");
  if (startHeatButton) { startHeat(Number(startHeatButton.dataset.startHeat)); return; }
  if (event.target.closest("[data-add-contestant]")) { document.querySelector("#contestant-list").insertAdjacentHTML("beforeend", contestantRow()); updateSchedulePreview(); return; }
  const removeButton = event.target.closest("[data-remove-contestant]");
  if (removeButton) { const list = document.querySelector("#contestant-list"); if (list.children.length <= 2) notify("A race needs at least two racers.", true); else { removeButton.closest(".contestant-config-row").remove(); updateSchedulePreview(); } return; }
  if (event.target.closest("[data-retry]")) loadState();
  if (event.target.closest("[data-index-retry]")) loadTournamentIndex();
});

document.addEventListener("submit", (event) => {
  if (event.target.matches("#config-form")) saveConfiguration(event);
  if (event.target.matches("#new-tournament-form")) createTournament(event);
});

tournamentSwitcher.addEventListener("change", async (event) => {
  activeTournamentId = Number(event.target.value);
  activeDay = 1;
  await loadState(activeTournamentId);
});

document.addEventListener("input", (event) => {
  if (event.target.closest("#config-form") && ["days", "heatsPerRacerPerDay", "maxMarblesPerHeat", "marblesPerRacer"].includes(event.target.name)) updateSchedulePreview();
});

document.addEventListener("fullscreenchange", () => {
  if (kioskMode && kioskUsesBrowserFullscreen && !document.fullscreenElement) setKioskMode(false);
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") startLiveUpdates();
  else stopLiveUpdates();
});

if (window.location.pathname === "/workspace") {
  landingPage.hidden = true;
  workspaceShell.hidden = false;
  document.body.className = kioskMode ? "workspace-mode kiosk-mode" : "workspace-mode";
  loadState();
} else {
  landingPage.hidden = false;
  workspaceShell.hidden = true;
  document.body.className = "landing-mode";
  document.title = "RollRank · Marble tournament control";
  loadTournamentIndex();
}
