let state = null;
const initialParams = new URLSearchParams(window.location.search);
const supportedViews = ["dashboard", "heats", "standings", "setup"];
let activeView = supportedViews.includes(initialParams.get("view")) ? initialParams.get("view") : "dashboard";
let activeDay = 1;
let activeTournamentId = Number(initialParams.get("tournament")) || null;
let kioskMode = initialParams.get("display") === "kiosk";
let liveEventSource = null;
let liveEventTournamentId = null;
let kioskIntroTimer = null;
let kioskResultsTimer = null;
let kioskCupTimer = null;
let kioskRefreshFailed = false;
let kioskLastUpdated = null;
let kioskUsesBrowserFullscreen = false;
let setupWizardStep = 0;
let setupWizardDraft = null;
let standingsTierView = "projected";
let tieBreakOrder = [];
let tieBreakKey = null;
if (kioskMode) activeView = "dashboard";

const landingPage = document.querySelector("#landing-page");
const workspaceShell = document.querySelector("#workspace-shell");
const tournamentIndexList = document.querySelector("#tournament-index-list");
const app = document.querySelector("#app");
const toast = document.querySelector("#toast");
const tournamentSwitcher = document.querySelector("#tournament-switcher");
const tournamentDialog = document.querySelector("#new-tournament-dialog");
const tieBreakDialog = document.querySelector("#tie-break-dialog");

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

let fieldHelpCounter = 0;
const fieldHelp = (bodyHtml) => {
  const id = `field-help-${fieldHelpCounter++}`;
  return `<span class="field-help-wrap"><button type="button" class="field-help-toggle" data-help-toggle="${id}" aria-expanded="false" aria-label="More information" aria-describedby="${id}">?</button><div class="field-help-popover" id="${id}" role="tooltip" hidden>${bodyHtml}</div></span>`;
};

function tiePopoverMarkup(id, racer, index, place, tiedWith) {
  const otherRacers = tiedWith
    .map((other) => `<span class="tie-popover-racer">${marble(other.color, "small")}${escapeHtml(other.name)}</span>`)
    .join("");
  return `<div class="tie-popover" id="${id}" role="tooltip" hidden><strong>Tied for ${ordinal(place)} in Round ${index + 1}</strong><p>${escapeHtml(racer.name)} matched ${tiedWith.length === 1 ? "1 other racer" : `${tiedWith.length} other racers`} on points and wins this round.</p><div class="tie-popover-racers">${otherRacers}</div><small>Order between tied racers was decided by roster order, not race results.</small></div>`;
}

function pendingTieBreakCallout() {
  const pending = state?.pendingTieBreak;
  if (!pending || kioskMode) return "";
  const names = pending.racers.map((racer) => escapeHtml(racer.name)).join(", ");
  return `<div class="config-callout tie-break-callout"><span aria-hidden="true">!</span><div><strong>Round ${pending.day} needs a tiebreak</strong><small>${names} finished this round tied on points, wins, and placement -- and how that's ordered decides who gets promoted. Racing can't continue until it's resolved.</small></div><button type="button" class="primary-button compact" data-open-tie-break>Resolve tie</button></div>`;
}

function kioskPendingTieBreakBanner() {
  const pending = state?.pendingTieBreak;
  if (!pending) return "";
  const names = pending.racers.map((racer) => escapeHtml(racer.name)).join(", ");
  return `<div class="kiosk-tiebreak-banner"><span aria-hidden="true">!</span><div><strong>Round ${pending.day} tiebreak pending</strong><p>${names} finished tied on points, wins, and placement. Racing is paused until it's resolved from the scoring device.</p></div></div>`;
}

function tieBreakDialogBody() {
  const pending = state?.pendingTieBreak;
  if (!pending) return "";
  const picked = new Set(tieBreakOrder);
  const remaining = pending.racers.filter((racer) => !picked.has(racer.id));
  const seatNoun = pending.racers.length > 2 ? "seats" : "seat";
  const orderMarkup = tieBreakOrder.length
    ? `<ol class="tie-break-order">${tieBreakOrder
        .map((id) => pending.racers.find((racer) => racer.id === id))
        .map((racer) => `<li>${marble(racer.color, "small")}<span>${escapeHtml(racer.name)}</span></li>`)
        .join("")}</ol>`
    : "";
  const picksMarkup = remaining.length
    ? `<div class="tie-break-picks">${remaining
        .map((racer) => `<button type="button" class="tie-break-pick" data-tie-pick="${racer.id}">${marble(racer.color, "small")}<span>${escapeHtml(racer.name)}</span><small>${racer.currentTier ? `Currently ${tierLabel[racer.currentTier]}` : "Currently no seat"}</small></button>`)
        .join("")}</div>`
    : "";
  return `<header><div><p class="eyebrow">Round ${pending.day} tiebreak</p><h2 id="tie-break-dialog-title">Resolve the tie for Round ${pending.day}</h2></div><button type="button" data-close-tie-break aria-label="Close">×</button></header><p>These racers matched on points, wins, and placement this round. Click them below in the order they should rank, highest first, to decide who wins the ${seatNoun} on the line.</p>${orderMarkup}${picksMarkup}<div class="dialog-actions"><button type="button" class="secondary-button" data-tie-break-reset ${tieBreakOrder.length ? "" : "disabled"}>Reset</button><button type="button" class="primary-button" data-tie-break-confirm ${tieBreakOrder.length === pending.racers.length ? "" : "disabled"}>Confirm order</button></div>`;
}

function syncTieBreakDialog() {
  if (!tieBreakDialog) return;
  const pending = state?.pendingTieBreak;
  const body = tieBreakDialog.querySelector("#tie-break-dialog-body");
  if (!pending) {
    tieBreakOrder = [];
    tieBreakKey = null;
    if (tieBreakDialog.open) tieBreakDialog.close();
    body.innerHTML = "";
    return;
  }
  const key = `${pending.day}:${pending.racers.map((racer) => racer.id).sort().join(",")}`;
  if (key !== tieBreakKey) {
    tieBreakOrder = [];
    tieBreakKey = key;
  }
  body.innerHTML = tieBreakDialogBody();
}

async function confirmTieBreak() {
  const pending = state?.pendingTieBreak;
  if (!pending || tieBreakOrder.length !== pending.racers.length) return;
  try {
    applyState(
      await api(`/api/tournaments/${state.competition.id}/staging/${pending.day}/tiebreak`, {
        method: "PUT",
        body: JSON.stringify({order: tieBreakOrder}),
      })
    );
    notify("Tie resolved.");
  } catch (error) { notify(error.message, true); }
}

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
  const introductionHeat = kioskMode ? newlyStartedHeat(state, nextState) : null;
  const completedHeat = kioskMode ? newlyCompletedHeat(state, nextState) : null;
  if (state && state.competition.id !== nextState.competition.id) stopKioskRaceAnimations();
  state = nextState;
  activeTournamentId = state.competition.id;
  if (activeDay !== "championship") activeDay = Math.min(activeDay, state.competition.days);
  kioskRefreshFailed = false;
  kioskLastUpdated = new Date();
  syncTournamentChrome();
  syncTieBreakDialog();
  render();
  if (completedHeat?.stage === "final") startKioskCupPresentation(completedHeat, state.competition.name);
  else if (completedHeat) startKioskRaceResults(completedHeat);
  else if (introductionHeat) startKioskRaceIntroduction(introductionHeat);
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

function tournamentHeats(snapshot) {
  if (!snapshot) return [];
  const championship = snapshot.championship;
  const heats = snapshot.days.flatMap((day) => day.heats)
    .concat(championship.wildcard.heats, championship.preliminary.heats);
  if (championship.final.heat) heats.push(championship.final.heat);
  return heats;
}

function newlyStartedHeat(previousState, nextState) {
  if (!previousState || previousState.competition.id !== nextState.competition.id) return null;
  const previousHeats = new Map(tournamentHeats(previousState).map((heat) => [heat.id, heat]));
  return tournamentHeats(nextState)
    .filter((heat) => heat.started && !previousHeats.get(heat.id)?.started)
    .sort((first, second) => first.globalNumber - second.globalNumber)[0] || null;
}

function newlyCompletedHeat(previousState, nextState) {
  if (!previousState || previousState.competition.id !== nextState.competition.id) return null;
  const previousHeats = new Map(tournamentHeats(previousState).map((heat) => [heat.id, heat]));
  return tournamentHeats(nextState)
    .filter((heat) => previousHeats.has(heat.id) && heat.complete && !previousHeats.get(heat.id).complete)
    .sort((first, second) => first.globalNumber - second.globalNumber)[0] || null;
}

function championshipHeatLabel(stage, heatNumber, prefix = "") {
  const heats = state.championship[stage] && state.championship[stage].heats;
  const label = heats && heats.length > 1 ? `Heat ${heatNumber}` : "Heat";
  return prefix ? `${prefix} ${label}` : label;
}

function kioskIntroductionHeatName(heat) {
  if (heat.stage === "wildcard") return `Championship: Wildcard ${championshipHeatLabel("wildcard", heat.heatNumber)}`;
  if (heat.stage === "preliminary") return `Championship: Preliminary ${championshipHeatLabel("preliminary", heat.heatNumber)}`;
  if (heat.stage === "final") return "Championship: Final";
  return `Round ${heat.day} · Heat ${heat.heatNumber}`;
}

function stopKioskRaceIntroduction() {
  if (kioskIntroTimer) window.clearTimeout(kioskIntroTimer);
  kioskIntroTimer = null;
  document.querySelector(".kiosk-race-intro")?.remove();
}

function stopKioskRaceResults() {
  if (kioskResultsTimer) window.clearTimeout(kioskResultsTimer);
  kioskResultsTimer = null;
  document.querySelector(".kiosk-race-results")?.remove();
}

function stopKioskCupPresentation() {
  if (kioskCupTimer) window.clearTimeout(kioskCupTimer);
  kioskCupTimer = null;
  document.querySelector(".kiosk-cup-presentation")?.remove();
}

function stopKioskRaceAnimations() {
  stopKioskRaceIntroduction();
  stopKioskRaceResults();
  stopKioskCupPresentation();
}

function randomizedKioskIntroDropRanks(entryCount) {
  const entryIndexes = Array.from({length:entryCount}, (_value, index) => index);
  for (let index = entryIndexes.length - 1; index > 0; index -= 1) {
    const swapIndex = Math.floor(Math.random() * (index + 1));
    [entryIndexes[index], entryIndexes[swapIndex]] = [entryIndexes[swapIndex], entryIndexes[index]];
  }
  const dropRanks = Array(entryCount);
  entryIndexes.forEach((entryIndex, dropRank) => { dropRanks[entryIndex] = dropRank; });
  return dropRanks;
}

function startKioskRaceIntroduction(heat) {
  if (!kioskMode || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  stopKioskRaceResults();
  stopKioskCupPresentation();
  stopKioskRaceIntroduction();
  const dropRanks = randomizedKioskIntroDropRanks(heat.entries.length);
  const dropStagger = heat.entries.length > 1 ? Math.min(.105, .78 / (heat.entries.length - 1)) : 0;
  const overlay = document.createElement("section");
  overlay.className = "kiosk-race-intro";
  overlay.setAttribute("aria-label", `Race introduction for ${kioskIntroductionHeatName(heat)}`);
  overlay.innerHTML = `
    <div class="kiosk-intro-stage kiosk-intro-title">
      <p>Now racing</p>
      <h2>${escapeHtml(kioskIntroductionHeatName(heat))}</h2>
    </div>
    <div class="kiosk-intro-stage kiosk-intro-marks">
      <h2>On your marks!</h2>
    </div>
    <div class="kiosk-intro-stage kiosk-intro-lineup">
      <p>Marbles to the gate</p>
      <div class="kiosk-intro-racers">
        ${heat.entries.map((entry, index) => {
          const direction = Math.random() < .5 ? -1 : 1;
          const spin = Math.round((420 + Math.random() * 360) * direction);
          const firstBounce = (18 + Math.random() * 13).toFixed(1);
          const secondBounce = (5 + Math.random() * 7).toFixed(1);
          return `<article style="--intro-drop-delay:${(dropRanks[index] * dropStagger).toFixed(3)}s;--intro-drop-spin:${spin}deg;--intro-first-bounce:-${firstBounce}px;--intro-second-bounce:-${secondBounce}px"><span class="kiosk-intro-marble-drop">${marble(entry.color)}</span><strong>${escapeHtml(entry.name)}</strong></article>`;
        }).join("")}
      </div>
      <i class="kiosk-intro-start-line" aria-hidden="true"></i>
    </div>
    <div class="kiosk-intro-stage kiosk-intro-go">
      <div class="kiosk-intro-flag" aria-hidden="true"><i></i><span></span></div>
      <h2>Go!</h2>
    </div>`;
  document.body.appendChild(overlay);
  kioskIntroTimer = window.setTimeout(stopKioskRaceIntroduction, 9400);
}

function heatMarblesInFinishOrder(heat) {
  const racers = heat.entries.flatMap((entry) => entry.marbles.map((result) => ({
    name: entry.name,
    color: entry.color,
    marbleNumber: result.number,
    showMarbleNumber: entry.marbles.length > 1,
    finish: Number(result.finish),
    lane: entry.lane,
  })));
  return racers.sort((first, second) => {
    if (first.finish === 0 && second.finish !== 0) return 1;
    if (first.finish !== 0 && second.finish === 0) return -1;
    if (first.finish !== second.finish) return first.finish - second.finish;
    return first.lane - second.lane || first.marbleNumber - second.marbleNumber;
  });
}

function randomKioskDnfEffect() {
  const effects = ["crash", "fire", "explode"];
  return effects[Math.floor(Math.random() * effects.length)];
}

function startKioskRaceResults(heat) {
  if (!kioskMode || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  stopKioskRaceIntroduction();
  stopKioskCupPresentation();
  stopKioskRaceResults();
  const orderedResults = heatMarblesInFinishOrder(heat);
  const finisherCount = orderedResults.filter((result) => result.finish > 0).length;
  const dnfCount = orderedResults.length - finisherCount;
  const finishGap = finisherCount > 1 ? Math.min(5.2, 33 / (finisherCount - 1)) : 0;
  const dnfBandCount = Math.ceil(dnfCount / 2);
  const dnfBandStep = dnfBandCount > 1 ? 20 / (dnfBandCount - 1) : 0;
  let finisherIndex = 0;
  let dnfIndex = 0;
  const results = orderedResults.map((result) => {
    if (result.finish > 0) {
      const presentation = {
        ...result,
        finisherIndex,
        finishStop: 90 - finisherIndex * finishGap,
        resultY: 56,
      };
      finisherIndex += 1;
      return presentation;
    }
    const dnfBandIndex = Math.floor(dnfIndex / 2);
    const resultY = dnfIndex % 2 ? 84 - dnfBandIndex * dnfBandStep : 16 + dnfBandIndex * dnfBandStep;
    const presentation = {
      ...result,
      dnfEffect: randomKioskDnfEffect(),
      dnfExitY: dnfIndex % 2 ? "78vh" : "-78vh",
      dnfSpin: dnfIndex % 2 ? "820deg" : "-820deg",
      resultY,
    };
    dnfIndex += 1;
    return presentation;
  });
  const densityClass = results.length > 18 ? " crowded" : results.length > 10 ? " compact" : "";
  const resultDelayStep = Math.min(170, Math.floor(3500 / Math.max(1, results.length - 1)));
  const overlay = document.createElement("section");
  overlay.className = `kiosk-race-results${densityClass}`;
  overlay.setAttribute("aria-label", `Race results for ${kioskIntroductionHeatName(heat)}`);
  overlay.innerHTML = `
    <header>
      <p>Official result</p>
      <h2>${escapeHtml(kioskIntroductionHeatName(heat))}</h2>
    </header>
    <div class="kiosk-results-track">
      <i class="kiosk-results-finish-line" aria-hidden="true"><span>Finish</span></i>
      <div class="kiosk-results-chute-positions" aria-hidden="true">
        ${results.filter((result) => result.finish > 0).map((result) => `<span style="--finish-stop:${result.finishStop.toFixed(3)}vw"><b>${result.finish}</b></span>`).join("")}
      </div>
      ${results.map((result, index) => `<article class="kiosk-result-racer${result.finish === 0 ? ` dnf dnf-${result.dnfEffect}` : ` finisher label-${result.finisherIndex % 2 ? "far" : "near"}`}" style="--result-y:${result.resultY.toFixed(3)}%;--result-delay:${index * resultDelayStep}ms;${result.finish > 0 ? `--finish-stop:${result.finishStop.toFixed(3)}vw` : `--dnf-exit-y:${result.dnfExitY};--dnf-spin:${result.dnfSpin}`}">
        ${marble(result.color)}
        <strong>${escapeHtml(result.name)}${result.showMarbleNumber ? ` <small>· Marble ${result.marbleNumber}</small>` : ""}</strong>
        <b>${result.finish === 0 ? "DNF" : ordinal(result.finish)}</b>
        ${result.finish === 0 ? `<i class="kiosk-result-dnf-effect" aria-hidden="true"></i>` : ""}
      </article>`).join("")}
    </div>`;
  document.body.appendChild(overlay);
  kioskResultsTimer = window.setTimeout(stopKioskRaceResults, 10000);
}

function kioskCupTiming(position) {
  const base = {3: 0, 2: 8, 1: 16}[position];
  return {
    announcement: `${base + .55}s`,
    racer: `${base + 2.65}s`,
    presenter: `${base + 4.45}s`,
    trophy: `${base + 6.25}s`,
  };
}

function kioskCupAnnouncement(racer, position, tournamentName) {
  const timing = kioskCupTiming(position);
  const introduction = position === 1
    ? `And your ${escapeHtml(tournamentName)} Champion`
    : `In ${ordinal(position)} place`;
  return `<div class="kiosk-cup-announcement${position === 1 ? " champion" : ""}" style="--announcement-delay:${timing.announcement}">
    <p>${introduction}</p>
    <h2>${escapeHtml(racer.name)}!</h2>
  </div>`;
}

function kioskCupPodiumPlace(racer, position) {
  const placeMeta = {
    1: {className: "gold", label: "Champion", trophy: "🏆", racerIn: "50vw", racerMid: "12vw", presenterIn: "-55vw", presenterOut: "52vw"},
    2: {className: "silver", label: "Second place", trophy: "🥈", racerIn: "-52vw", racerMid: "-13vw", presenterIn: "50vw", presenterOut: "-50vw"},
    3: {className: "bronze", label: "Third place", trophy: "🥉", racerIn: "52vw", racerMid: "13vw", presenterIn: "-50vw", presenterOut: "48vw"},
  }[position];
  if (!racer) return `<article class="kiosk-cup-place ${placeMeta.className} empty"><div class="kiosk-cup-step"><b>${position}</b><span>${placeMeta.label}</span></div></article>`;
  const timing = kioskCupTiming(position);
  return `<article class="kiosk-cup-place ${placeMeta.className}" style="--racer-delay:${timing.racer};--presenter-delay:${timing.presenter};--trophy-delay:${timing.trophy};--racer-in:${placeMeta.racerIn};--racer-mid:${placeMeta.racerMid};--presenter-in:${placeMeta.presenterIn};--presenter-out:${placeMeta.presenterOut}">
    <div class="kiosk-cup-racer">
      ${marble(racer.color)}
      <strong>${escapeHtml(racer.name)}</strong>
      <span class="kiosk-cup-winner-trophy" role="img" aria-label="${placeMeta.label} trophy">${placeMeta.trophy}</span>
    </div>
    <div class="kiosk-cup-presenter" aria-hidden="true">
      <div class="kiosk-cup-host">${marble("#172D52")}<i class="kiosk-cup-top-hat"></i></div>
      <span class="kiosk-cup-presenter-trophy">${placeMeta.trophy}</span>
    </div>
    <div class="kiosk-cup-step"><b>${position}</b><span>${placeMeta.label}</span></div>
  </article>`;
}

function startKioskCupPresentation(finalHeat, tournamentName) {
  if (!kioskMode || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  stopKioskRaceAnimations();
  const placedRacers = new Map(finalHeat.entries.filter((entry) => entry.finish > 0 && entry.finish <= 3).map((entry) => [Number(entry.finish), entry]));
  const sequence = [3, 2, 1].filter((position) => placedRacers.has(position));
  const overlay = document.createElement("section");
  overlay.className = "kiosk-cup-presentation";
  overlay.setAttribute("aria-label", `${tournamentName} cup presentation`);
  overlay.innerHTML = `
    ${fireworksMarkup("kiosk-cup-fireworks")}
    <header><p>Official cup presentation</p><strong>${escapeHtml(tournamentName)}</strong></header>
    ${sequence.map((position) => kioskCupAnnouncement(placedRacers.get(position), position, tournamentName)).join("")}
    <div class="kiosk-cup-stage">
      <div class="kiosk-cup-podium">
        ${[2, 1, 3].map((position) => kioskCupPodiumPlace(placedRacers.get(position), position)).join("")}
      </div>
    </div>`;
  document.body.appendChild(overlay);
  kioskCupTimer = window.setTimeout(stopKioskCupPresentation, 32000);
}

async function setKioskMode(enabled, requestBrowserFullscreen = false) {
  if (!enabled) stopKioskRaceAnimations();
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
  const extraClass = arguments[0] || "";
  return `<div class="kiosk-fireworks${extraClass ? ` ${extraClass}` : ""}" aria-hidden="true">
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
    ${pendingTieBreakCallout()}
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
          ${nextHeat.locked ? `<span class="heat-locked-note">Resolve the pending tiebreak first</span>` : nextHeat.started === false ? `<button class="primary-button compact" data-start-heat="${nextHeat.id}">Start heat</button>` : `<button class="primary-button compact" data-score-heat="${nextHeat.id}">Score heat</button>`}
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
  const finalizedTiers = racer.dayChampionshipPreviousTiers || tiers;
  const tierStat = (tier, key, label, title) => {
    const count = tiers.filter((item) => item === tier).length;
    const finalizedCount = finalizedTiers.filter((item) => item === tier).length;
    return {key, label, title, count, finalizedCount, live:count !== finalizedCount, liveAlreadyCounted:true};
  };
  const stats = [
    {key: "w", label: "W", title: "Wins", count: racer.wins, live: racer.liveRoundLeader, liveAlreadyCounted: false},
    tierStat("bye", "b", "F", "Marbles promoted to the final"),
    tierStat("preliminary", "p", "P", "Preliminary"),
    tierStat("wildcard", "wc", "WC", "Wildcard"),
  ];
  // Wins exclude the live round and need a provisional +1. Championship
  // counts already contain every projected reassignment, including removals,
  // so they only need an asterisk and a comparison with the finalized count.
  return stats
    .map((stat) => ({...stat, displayCount: stat.live && !stat.liveAlreadyCounted ? stat.count + 1 : stat.count}))
    .filter((stat) => stat.displayCount > 0 || (stat.live && stat.finalizedCount > 0));
}

function statBadgesMarkup(racer, wrapperClass, chipClass) {
  return `<div class="${wrapperClass}">${racerStatBadges(racer).map((stat) => `<span class="${chipClass} stat-${stat.key}" title="${stat.title}${stat.live ? stat.finalizedCount == null ? " · leading, not yet finalized" : ` · projected from ${stat.finalizedCount}` : ""}">${stat.displayCount}${stat.live ? "*" : ""}<small>${stat.label}</small></span>`).join("")}</div>`;
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
  const visibleStandings = state.standings.slice(0, 8);
  const completedRounds = state.days.filter((day) => day.heats.every((heat) => heat.complete)).length;
  const progress = progressPercent();
  const roundDividers = [];
  let heatsSoFar = 0;
  for (let i = 0; i < state.days.length - 1; i++) {
    heatsSoFar += state.days[i].heats.length;
    if (c.totalHeats) roundDividers.push(Math.round((heatsSoFar / c.totalHeats) * 100));
  }
  const liveStatus = kioskRefreshFailed ? "Reconnecting…" : kioskTimestamp();
  let nextContent = "";

  if (state.pendingTieBreak && nextHeat?.locked) {
    nextContent = `<p class="kiosk-card-label">Paused</p><div class="kiosk-final-ready"><span aria-hidden="true">!</span><div><strong>Round ${state.pendingTieBreak.day} tiebreak pending</strong><p>Racing resumes once it's resolved from the scoring device.</p></div></div>`;
  } else if (nextHeat) {
    nextContent = `<div class="kiosk-card-heading"><p class="kiosk-card-label">${nextHeat.started ? "In progress" : "Up next"}</p><b>Race #${nextHeat.globalNumber}</b></div><h2>Round ${nextHeat.day} · Heat ${nextHeat.heatNumber}</h2><div class="kiosk-next-racers">${nextHeat.entries.map((entry) => `<span>${marble(entry.color, "small")}<b>${escapeHtml(entry.name)}</b></span>`).join("")}</div>`;
  } else if (nextWildcard) {
    nextContent = `<div class="kiosk-card-heading"><p class="kiosk-card-label">${nextWildcard.started ? "In progress" : "Up next"}</p><b>Championship</b></div><h2>Championship: Wildcard ${championshipHeatLabel("wildcard", nextWildcard.heatNumber)}</h2><div class="kiosk-next-racers">${nextWildcard.entries.map((entry) => `<span>${marble(entry.color, "small")}<b>${escapeHtml(entry.name)}</b></span>`).join("")}</div>`;
  } else if (nextPreliminary) {
    nextContent = `<div class="kiosk-card-heading"><p class="kiosk-card-label">${nextPreliminary.started ? "In progress" : "Up next"}</p><b>Championship</b></div><h2>Championship: Preliminary ${championshipHeatLabel("preliminary", nextPreliminary.heatNumber)}</h2><div class="kiosk-next-racers">${nextPreliminary.entries.map((entry) => `<span>${marble(entry.color, "small")}<b>${escapeHtml(entry.name)}</b></span>`).join("")}</div>`;
  } else {
    nextContent = `<p class="kiosk-card-label">Up next</p><div class="kiosk-final-ready"><span>★</span><div><strong>Championship in progress</strong><p>Check the bracket for the current stage.</p></div></div>`;
  }

  return `<section class="kiosk-dashboard" aria-label="Live tournament dashboard">
    <header class="kiosk-header">
      <a class="kiosk-brand" href="/" aria-label="RollRank home"><img src="/static/marble-logo.png" alt="" width="48" height="48"><span><strong>RollRank</strong><small>Live tournament display</small></span></a>
      <div class="kiosk-title"><p>${status.label}</p><h1>${escapeHtml(c.name)}</h1></div>
      <div class="kiosk-controls"><span class="kiosk-live-status${kioskRefreshFailed ? " stale" : ""}"><i></i>${liveStatus}</span><button type="button" data-exit-kiosk aria-label="Exit fullscreen display">Exit <span aria-hidden="true">×</span></button></div>
    </header>
    ${kioskPendingTieBreakBanner()}
    <div class="kiosk-overview">
      <article class="kiosk-progress-card">
        <div class="kiosk-card-heading"><p class="kiosk-card-label">Round progress</p></div>
        <div class="kiosk-progress-value"><strong>${progress}%<small>[${c.completedHeats} / ${c.totalHeats}]</small></strong><span>${completedRounds} of ${c.days} rounds complete</span></div>
        <div class="kiosk-progress-track"><i style="width:${progress}%"></i>${roundDividers.map((pct) => `<span class="kiosk-progress-divider" style="left:${pct}%"></span>`).join("")}</div>
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

function heatTitleMarkup(heat) {
  return `<div class="heat-title"><span>Heat</span><strong>${heat.heatNumber}</strong><small>Race #${heat.globalNumber}</small></div>`;
}

function lockedHeatCard(heat) {
  return `<article class="heat-card locked" id="heat-${heat.id}">
    <header class="heat-card-heading">
      ${heatTitleMarkup(heat)}
      <div><span class="pending-chip">Locked</span></div>
      <span class="heat-locked-note">Complete the earlier rounds first</span>
    </header>
    ${heatRacerPreview(heat)}
  </article>`;
}

function readyToStartHeatCard(heat) {
  return `<article class="heat-card ready" id="heat-${heat.id}">
    <header class="heat-card-heading">
      ${heatTitleMarkup(heat)}
      <div><span class="pending-chip">Ready to start</span></div>
      <button class="primary-button compact" data-start-heat="${heat.id}">Start heat</button>
    </header>
    ${heatRacerPreview(heat)}
  </article>`;
}

function editLockedHeatCard(heat) {
  return `<article class="heat-card complete locked" id="heat-${heat.id}">
    <header class="heat-card-heading">
      ${heatTitleMarkup(heat)}
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
      ${heatTitleMarkup(heat)}
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
    ${pendingTieBreakCallout()}
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
  const label = sourceHeats.length === 1
    ? `${stageLabel} Heat`
    : heatNumbers.length === 1
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

function ladderProjectedRoster(entries, lockedLabel, sourceStageLabel) {
  return `<div class="ladder-heat projected">
    <div class="ladder-heat-head"><span aria-hidden="true">🔒</span><span>Not yet locked in</span></div>
    <ul class="ladder-entries">
      ${entries.map((entry) => entry.decided
        ? `<li class="ladder-entry ${tierClass(entry.originStage === "wildcard" ? "wildcard" : entry.originStage === "bye" ? "bye" : "preliminary")}">${marble(entry.color, "small")}${ladderEntryLabel(entry.name, entry.marbleSlots || 1, entry.seedRounds)}</li>`
        : `<li class="ladder-entry pending"><span class="tbd-marble" aria-hidden="true">?</span><span>${championshipHeatLabel(entry.originStage, entry.heatNumber, sourceStageLabel)}${entry.qualifyingPlace ? ` · ${ordinal(entry.qualifyingPlace)} racer` : " winner"}</span><b>TBD</b></li>`
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

function ladderHeatCard(heat, heatCount, sourceHeats, sourceStageLabel) {
  return `<article class="ladder-heat ${heat.complete ? "complete" : ""}">
    <div class="ladder-heat-head"><span>${heatCount > 1 ? `Heat ${heat.heatNumber}` : "Heat"}</span><span class="${heat.complete ? "complete-chip" : "pending-chip"}">${heat.complete ? "Complete" : "In progress"}</span></div>
    <ul class="ladder-entries">
      ${heat.entries.map((entry) => `<li class="ladder-entry${entry.finish === 0 ? " dnf" : ""}">
        ${marble(entry.color, "small")}${ladderEntryLabel(entry.name, entry.marbles.length, entry.seedRounds, seedHeatTag(entry, sourceHeats, sourceStageLabel))}<b>${ladderPlaceLabel(entry)}</b>
      </li>`).join("")}
    </ul>
  </article>`;
}

function ladderStageColumn(stage, lockedLabel, sourceHeats, sourceStageLabel) {
  if (!stage.ready) {
    if (stage.projectedEntries && stage.projectedEntries.length) return ladderProjectedRoster(stage.projectedEntries, lockedLabel, sourceStageLabel);
    return ladderLockedPlaceholder(lockedLabel);
  }
  if (stage.skipped) return ladderSkippedPlaceholder(stage);
  return `<div class="ladder-heats">${stage.heats.map((heat) => ladderHeatCard(heat, stage.heats.length, sourceHeats, sourceStageLabel)).join("")}</div>`;
}

function ladderFinalColumn(finalStage, sourceHeats) {
  if (!finalStage.ready) {
    if (finalStage.projectedEntries && finalStage.projectedEntries.length) {
      return ladderProjectedRoster(finalStage.projectedEntries, "Preliminary results decide the final field.", "Preliminary");
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
    <div class="panel-heading"><div><h2>Championship ladder</h2></div></div>
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

function standingRoundCell(racer, place, index, liveRoundDay, mobile = false) {
  const projectedTier = racer.dayChampionshipTiers[index];
  const previousTier = (racer.dayChampionshipPreviousTiers || [])[index];
  const showProjected = standingsTierView === "projected";
  const tier = showProjected ? projectedTier : previousTier;
  const provisional = showProjected && (racer.dayChampionshipTierProvisional || [])[index] === true;
  let tierMarkup = tier ? `<small class="tier-tag">${tierLabel[tier]}</small>` : "";
  if (provisional) {
    const transition = tier && previousTier
      ? `${tierLabel[previousTier]} → ${tierLabel[tier]}*`
      : tier
        ? `Projected ${tierLabel[tier]}*`
        : `${tierLabel[previousTier]} → No seat*`;
    tierMarkup = `<small class="tier-tag provisional" title="Provisional reassignment while round ${liveRoundDay} is in progress">${transition}</small>`;
  }
  const className = `${tierClass(tier)}${provisional ? ` tier-reassigned${tier ? "" : " tier-removed"}` : ""}`;
  const placement = placeLabel(place, index + 1 === liveRoundDay);
  const tiedWith = place != null ? (racer.dayTiedWith || [])[index] : null;
  if (!tiedWith || tiedWith.length === 0) {
    return mobile
      ? `<span class="${className}"><small>Round ${index + 1}</small><b>${placement}</b>${tierMarkup}</span>`
      : `<td class="${className}">${placement}${tierMarkup}</td>`;
  }
  const id = `tie-popover-${racer.id}-${index}-${mobile ? "mobile" : "desktop"}`;
  const flag = `<sup class="tie-flag" aria-hidden="true">!</sup>`;
  const popover = tiePopoverMarkup(id, racer, index, place, tiedWith);
  const buttonAttrs = `type="button" class="tie-cell-toggle" data-tie-toggle="${id}" aria-expanded="false" aria-describedby="${id}" aria-label="Tied placement, view details"`;
  return mobile
    ? `<span class="${className} tie-target"><button ${buttonAttrs}><small>Round ${index + 1}</small><b>${placement}${flag}</b>${tierMarkup}</button>${popover}</span>`
    : `<td class="${className} tie-target"><button ${buttonAttrs}>${placement}${flag}${tierMarkup}</button>${popover}</td>`;
}

function standingsTierViewControl(liveRoundDay) {
  const projected = standingsTierView === "projected";
  return `<div class="provisional-tier-note"><span>${projected ? "*" : "●"}</span><div class="provisional-tier-copy"><strong>${projected ? "Projected qualification changes" : "Current qualification status"}</strong><small>${projected ? `Round ${liveRoundDay} is in progress. Arrows show earlier seats that would be reassigned if the round ended now.` : `Showing finalized qualification seats only. Round ${liveRoundDay} placements remain provisional until every heat is scored.`}</small></div><div class="standings-tier-toggle" role="group" aria-label="Qualification seat view"><button type="button" data-standings-tier-view="current" aria-pressed="${!projected}" class="${projected ? "" : "active"}">Current</button><button type="button" data-standings-tier-view="projected" aria-pressed="${projected}" class="${projected ? "active" : ""}">Projected</button></div></div>`;
}

function renderStandings() {
  const c = state.competition;
  return `${viewHeader("Live scoring", "Tournament standings", "Round placings update automatically whenever a heat result is saved.")}
    ${pendingTieBreakCallout()}
    ${renderChampionshipLadder()}
    <section class="panel table-panel">${c.liveRoundDay ? standingsTierViewControl(c.liveRoundDay) : ""}<div class="table-scroll"><table class="standings-table"><thead><tr><th>Racer</th>${Array.from({length:c.days}, (_, i) => `<th>Round ${i + 1}</th>`).join("")}</tr></thead><tbody>
    ${state.standings.map((racer) => `<tr><td><span class="racer-cell">${marble(racer.color, "small")}<strong>${escapeHtml(racer.name)}</strong></span></td>${racer.dayPlacements.map((place, index) => standingRoundCell(racer, place, index, c.liveRoundDay)).join("")}</tr>`).join("")}
    </tbody></table></div>
    <div class="mobile-standings" aria-label="Mobile standings">
      ${state.standings.map((racer) => `<article class="mobile-standing-card">
        <div class="mobile-standing-lead">${marble(racer.color, "small")}<strong>${escapeHtml(racer.name)}</strong></div>
        <div class="mobile-standing-stats">${racer.dayPlacements.map((place, index) => standingRoundCell(racer, place, index, c.liveRoundDay, true)).join("")}</div>
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
  return `<div class="contestant-config-row"><span class="drag-handle" aria-hidden="true">⋮⋮</span><span class="contestant-color-marble">${marble(contestant.color)}<input type="color" value="${escapeHtml(contestant.color)}" aria-label="Marble color"></span><input type="text" value="${escapeHtml(contestant.name)}" maxlength="50" aria-label="Racer name" required><button type="button" data-remove-contestant aria-label="Remove racer">×</button></div>`;
}

const wizardSteps = ["Format", "Rounds", "Championship", "Review"];

function boundedNumber(value, minimum, maximum, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.min(maximum, Math.max(minimum, Math.round(number))) : fallback;
}

function setupWizardValuesFromForm() {
  const form = document.querySelector("#config-form");
  const value = (name) => Number(form.elements[name].value);
  const checked = (name) => form.elements[name].checked;
  return {
    preset:"custom",
    days:value("days"), heatsPerRacerPerDay:value("heatsPerRacerPerDay"),
    maxMarblesPerHeat:value("maxMarblesPerHeat"), marblesPerRacer:value("marblesPerRacer"),
    wildcardMaxMarblesPerHeat:value("wildcardMaxMarblesPerHeat"),
    preliminaryMaxMarblesPerHeat:value("preliminaryMaxMarblesPerHeat"),
    maxFinalByeMarblesPerRacer:value("maxFinalByeMarblesPerRacer"),
    maxPrelimMarblesForRacerWithFinalBye:value("maxPrelimMarblesForRacerWithFinalBye"),
    maxWildcardMarblesForRacerWithFinalBye:value("maxWildcardMarblesForRacerWithFinalBye"),
    allowCascadingFinalByeSelection:checked("allowCascadingFinalByeSelection"),
    maxPrelimPromotionMarblesPerRacer:value("maxPrelimPromotionMarblesPerRacer"),
    allowCascadingPrelimPromotionSelection:checked("allowCascadingPrelimPromotionSelection"),
    maxWildcardMarblesForRacerWithPrelimPromotion:value("maxWildcardMarblesForRacerWithPrelimPromotion"),
    maxWildcardPromotionMarblesPerRacer:value("maxWildcardPromotionMarblesPerRacer"),
    allowCascadingWildcardPromotionSelection:checked("allowCascadingWildcardPromotionSelection"),
    wildcardRacersPromotedPerHeat:value("wildcardRacersPromotedPerHeat"),
    preliminaryRacersPromotedPerHeat:value("preliminaryRacersPromotedPerHeat"),
    maxFinalRacers:value("maxFinalRacers"), scoringStyle:"keep",
  };
}

function wizardPreset(name) {
  const racerCount = document.querySelectorAll(".contestant-config-row").length;
  const shared = {
    preset:name, marblesPerRacer:1, maxPrelimMarblesForRacerWithFinalBye:0,
    maxWildcardMarblesForRacerWithFinalBye:0, maxWildcardMarblesForRacerWithPrelimPromotion:0,
    allowCascadingFinalByeSelection:true, allowCascadingPrelimPromotionSelection:true,
    allowCascadingWildcardPromotionSelection:true,
  };
  const presets = {
    express:{days:2, heatsPerRacerPerDay:1, maxMarblesPerHeat:6, wildcardMaxMarblesPerHeat:6, preliminaryMaxMarblesPerHeat:6, maxFinalByeMarblesPerRacer:1, maxPrelimPromotionMarblesPerRacer:1, maxWildcardPromotionMarblesPerRacer:1, wildcardRacersPromotedPerHeat:1, preliminaryRacersPromotedPerHeat:2, maxFinalRacers:4, scoringStyle:"podium"},
    classic:{days:3, heatsPerRacerPerDay:3, maxMarblesPerHeat:6, wildcardMaxMarblesPerHeat:6, preliminaryMaxMarblesPerHeat:6, maxFinalByeMarblesPerRacer:2, maxPrelimPromotionMarblesPerRacer:1, maxWildcardPromotionMarblesPerRacer:2, wildcardRacersPromotedPerHeat:2, preliminaryRacersPromotedPerHeat:2, maxFinalRacers:6, scoringStyle:"classic"},
    showcase:{days:5, heatsPerRacerPerDay:3, maxMarblesPerHeat:8, wildcardMaxMarblesPerHeat:8, preliminaryMaxMarblesPerHeat:8, maxFinalByeMarblesPerRacer:2, maxPrelimPromotionMarblesPerRacer:2, maxWildcardPromotionMarblesPerRacer:3, wildcardRacersPromotedPerHeat:3, preliminaryRacersPromotedPerHeat:3, maxFinalRacers:8, scoringStyle:"balanced"},
  };
  const selected = {...shared, ...presets[name]};
  selected.maxFinalRacers = Math.max(2, Math.min(racerCount, selected.maxFinalRacers));
  return selected;
}

function wizardHeatSize(fieldSize, maxMarbles, marblesPerRacer = 1, racerLimit = fieldSize) {
  const capacity = Math.min(fieldSize, racerLimit, 24, Math.floor(maxMarbles / marblesPerRacer));
  for (let size = capacity; size >= 2; size -= 1) {
    if (fieldSize % size === 0) return {size, count:fieldSize / size};
  }
  return {size:0, count:0};
}

function setupWizardProjection(draft = setupWizardDraft) {
  const racerCount = document.querySelectorAll(".contestant-config-row").length;
  const appearances = racerCount * draft.heatsPerRacerPerDay;
  const staging = wizardHeatSize(appearances, draft.maxMarblesPerHeat, draft.marblesPerRacer, racerCount);
  const stagingHeats = staging.count * draft.days;
  const byeMarbles = draft.maxFinalByeMarblesPerRacer ? Math.min(draft.days, racerCount * draft.maxFinalByeMarblesPerRacer) : 0;
  const directPrelim = draft.maxPrelimPromotionMarblesPerRacer ? Math.min(draft.days, racerCount * draft.maxPrelimPromotionMarblesPerRacer) : 0;
  const wildcardMarbles = draft.maxWildcardPromotionMarblesPerRacer ? Math.min(draft.days * 2, racerCount * draft.maxWildcardPromotionMarblesPerRacer) : 0;
  const wildcard = wizardHeatSize(wildcardMarbles, draft.wildcardMaxMarblesPerHeat);
  const wildcardAdvancers = wildcard.count ? Math.min(wildcardMarbles, wildcard.count * draft.wildcardRacersPromotedPerHeat) : wildcardMarbles;
  const preliminaryMarbles = directPrelim + wildcardAdvancers;
  const preliminary = wizardHeatSize(preliminaryMarbles, draft.preliminaryMaxMarblesPerHeat);
  const preliminaryAdvancers = preliminary.count ? Math.min(preliminaryMarbles, preliminary.count * draft.preliminaryRacersPromotedPerHeat) : preliminaryMarbles;
  const finalists = Math.min(racerCount, draft.maxFinalRacers, byeMarbles + preliminaryAdvancers);
  const championshipHeats = wildcard.count + preliminary.count + (finalists >= 2 ? 1 : 0);
  return {racerCount, staging, stagingHeats, byeMarbles, directPrelim, wildcardMarbles, wildcard, wildcardAdvancers, preliminaryMarbles, preliminary, preliminaryAdvancers, finalists, championshipHeats, totalHeats:stagingHeats + championshipHeats, valid:Boolean(staging.count)};
}

function wizardNumberControl(label, setting, minimum, maximum, help) {
  return `<label class="wizard-control"><span>${label}</span><input type="number" min="${minimum}" max="${maximum}" value="${setupWizardDraft[setting]}" data-wizard-setting="${setting}"><small>${help}</small></label>`;
}

function wizardStageMap(projection) {
  const skipped = (stage) => stage.count ? `${stage.count} heat${stage.count === 1 ? "" : "s"}` : "auto-advance";
  return `<div class="wizard-stage-map" aria-label="Projected tournament stages">
    <article><span>01</span><div><strong>${setupWizardDraft.days} staging rounds</strong><small>${projection.stagingHeats} heats · every racer appears ${setupWizardDraft.heatsPerRacerPerDay}× per round</small></div></article><i aria-hidden="true">→</i>
    <article><span>02</span><div><strong>Wildcard</strong><small>Up to ${projection.wildcardMarbles} marbles · ${skipped(projection.wildcard)}</small></div></article><i aria-hidden="true">→</i>
    <article><span>03</span><div><strong>Preliminary</strong><small>Up to ${projection.preliminaryMarbles} marbles · ${skipped(projection.preliminary)}</small></div></article><i aria-hidden="true">→</i>
    <article><span>04</span><div><strong>The final</strong><small>Up to ${projection.finalists} racers · one marble each</small></div></article>
  </div>`;
}

function wizardStagingImpact(projection) {
  return `<p class="eyebrow">Live impact</p>${projection.valid ? `<strong>${projection.stagingHeats}</strong><h4>staging heats total</h4><ul><li>${projection.staging.count} heats in each round</li><li>${projection.staging.size} racers per heat</li><li>${projection.staging.size * setupWizardDraft.marblesPerRacer} marbles on the track</li><li>${projection.stagingHeats * projection.staging.size * setupWizardDraft.marblesPerRacer} marble runs before the championship</li></ul>` : `<strong>!</strong><h4>No complete schedule fits</h4><p>Raise the track limit or change appearances so every heat can have the same number of racers.</p>`}`;
}

function wizardChampionshipImpact(projection) {
  return `<p class="eyebrow">Projected field</p><div class="wizard-impact-stats"><span><b>${projection.wildcardMarbles}</b> wildcard marbles</span><span><b>${projection.wildcard.count}</b> wildcard heats</span><span><b>${projection.preliminaryMarbles}</b> preliminary marbles</span><span><b>${projection.preliminary.count}</b> preliminary heats</span><span><b>${projection.finalists}</b> finalists</span></div><p class="wizard-caveat">Projections show the largest possible fields. Repeat racers and results can make stages smaller.</p>`;
}

function wizardCascadingMode(draft = setupWizardDraft) {
  const values = [draft.allowCascadingFinalByeSelection, draft.allowCascadingPrelimPromotionSelection, draft.allowCascadingWildcardPromotionSelection];
  if (values.every(Boolean)) return "spread";
  if (values.every((value) => !value)) return "reward";
  return "custom";
}

function wizardCascadingStatus() {
  const mode = wizardCascadingMode();
  if (mode === "custom") return `<div class="wizard-choice-status custom"><strong>Mixed expert settings</strong><span>The three cascading toggles currently differ. Choose an option above to turn all three on or off.</span></div>`;
  const enabled = mode === "spread";
  return `<div class="wizard-choice-status ${enabled ? "on" : "off"}"><strong>All cascading ${enabled ? "enabled" : "disabled"}</strong><span>Final byes · preliminary promotion · wildcard promotion</span></div>`;
}

function refreshSetupWizardFeedback() {
  const impact = document.querySelector("#setup-wizard .wizard-impact");
  if (!impact || !setupWizardDraft) return;
  const projection = setupWizardProjection();
  if (setupWizardStep === 1) {
    impact.className = `wizard-impact ${projection.valid ? "" : "invalid"}`;
    impact.innerHTML = wizardStagingImpact(projection);
  } else if (setupWizardStep === 2) {
    impact.innerHTML = wizardChampionshipImpact(projection);
  }
}

function renderSetupWizardStep() {
  const body = document.querySelector("#setup-wizard-body");
  const footer = document.querySelector("#setup-wizard-footer");
  if (!body || !footer || !setupWizardDraft) return;
  const projection = setupWizardProjection();
  document.querySelectorAll(".wizard-step").forEach((item, index) => {
    item.classList.toggle("active", index === setupWizardStep);
    item.classList.toggle("complete", index < setupWizardStep);
  });
  if (setupWizardStep === 0) {
    body.innerHTML = `<div class="wizard-intro"><p class="eyebrow">Choose a starting point</p><h3>How should this tournament feel?</h3><p>Start with a complete format, then tune every stage in the next steps.</p></div>
      <div class="wizard-presets">
        <button type="button" class="wizard-preset ${setupWizardDraft.preset === "express" ? "selected" : ""}" data-wizard-preset="express"><span>⚡</span><strong>Express</strong><small>2 rounds · fewer heats · compact final</small><em>Best for a quick event</em></button>
        <button type="button" class="wizard-preset ${setupWizardDraft.preset === "classic" ? "selected" : ""}" data-wizard-preset="classic"><span>🏁</span><strong>Classic</strong><small>3 rounds · full championship ladder</small><em>Balanced and familiar</em></button>
        <button type="button" class="wizard-preset ${setupWizardDraft.preset === "showcase" ? "selected" : ""}" data-wizard-preset="showcase"><span>🏆</span><strong>Showcase</strong><small>5 rounds · more racing · wider final</small><em>For a feature event</em></button>
      </div>
      <div class="wizard-current-note"><strong>${setupWizardDraft.preset === "custom" ? "Your current expert settings are loaded." : `${setupWizardDraft.preset[0].toUpperCase()}${setupWizardDraft.preset.slice(1)} format selected.`}</strong><span>You can change any recommendation before applying it.</span></div>`;
  } else if (setupWizardStep === 1) {
    body.innerHTML = `<div class="wizard-layout"><div><p class="eyebrow">Staging rounds</p><h3>Give every racer enough track time</h3><p class="wizard-lead">More rounds improve the chance that consistent racers rise to the top. More appearances per round reduce luck, but add heats.</p><div class="wizard-controls">
      ${wizardNumberControl("Race rounds", "days", 1, 30, "Each round produces a new set of championship qualifiers.")}
      ${wizardNumberControl("Heats per racer / round", "heatsPerRacerPerDay", 1, 20, "How often each racer appears in every round.")}
      ${wizardNumberControl("Marbles per racer / heat", "marblesPerRacer", 1, 20, "Extra marbles add scoring depth but reduce racers per heat.")}
      ${wizardNumberControl("Track marble limit", "maxMarblesPerHeat", 2, 480, "The maximum number of marbles your track can handle safely.")}
      </div></div><aside class="wizard-impact ${projection.valid ? "" : "invalid"}">${wizardStagingImpact(projection)}</aside></div>`;
  } else if (setupWizardStep === 2) {
    const cascadingMode = wizardCascadingMode();
    body.innerHTML = `<div class="wizard-layout championship"><div><p class="eyebrow">Championship ladder</p><h3>Control how racers advance</h3><p class="wizard-lead">Round winners receive final byes, runners-up enter the preliminary stage, and the next two places enter the wildcard stage.</p><div class="wizard-controls">
      ${wizardNumberControl("Wildcard marble limit / racer", "maxWildcardPromotionMarblesPerRacer", 0, 20, "Set to 0 to skip wildcard qualification; higher values reward repeat results.")}
      ${wizardNumberControl("Promoted / wildcard heat", "wildcardRacersPromotedPerHeat", 1, 24, "Top racers from each wildcard heat who reach the preliminary stage.")}
      ${wizardNumberControl("Max marbles / wildcard heat", "wildcardMaxMarblesPerHeat", 2, 480, "Lower limits can create more wildcard heats.")}
      ${wizardNumberControl("Preliminary marble limit / racer", "maxPrelimPromotionMarblesPerRacer", 0, 20, "Set to 0 to disable direct runner-up promotion.")}
      ${wizardNumberControl("Promoted / preliminary heat", "preliminaryRacersPromotedPerHeat", 1, 24, "Top racers from each preliminary heat who reach the final.")}
      ${wizardNumberControl("Max marbles / preliminary heat", "preliminaryMaxMarblesPerHeat", 2, 480, "Lower limits can create more preliminary heats.")}
      ${wizardNumberControl("Max racers in final", "maxFinalRacers", 2, Math.min(24, projection.racerCount), "The final is trimmed to this many unique racers.")}
      </div><fieldset class="wizard-choice"><legend>Qualification seat behavior</legend><label><input type="radio" name="wizard-repeat" value="spread" data-wizard-repeat ${cascadingMode === "spread" ? "checked" : ""}><span><strong>Spread opportunities <em>Cascading on</em></strong><small>When a racer is capped, pass final-bye, preliminary, and wildcard seats down the standings.</small></span></label><label><input type="radio" name="wizard-repeat" value="reward" data-wizard-repeat ${cascadingMode === "reward" ? "checked" : ""}><span><strong>Reward repeat performers <em>No cascading</em></strong><small>Disable all three cascading toggles. When a racer is capped, that qualification seat is forfeited.</small></span></label>${wizardCascadingStatus()}</fieldset></div><aside class="wizard-impact">${wizardChampionshipImpact(projection)}</aside></div>`;
  } else {
    const scoreLabels = {keep:"Keep current scoring", classic:"Classic 10–7–5 curve", balanced:"Every place scores", podium:"Podium-focused"};
    const cascadingMode = wizardCascadingMode();
    const cascadingTitle = cascadingMode === "spread" ? "Spread opportunities" : cascadingMode === "reward" ? "Reward repeat performers" : "Mixed expert settings";
    const cascadingDescription = cascadingMode === "spread" ? "Cascading enabled for all three qualification tiers" : cascadingMode === "reward" ? "No cascading; capped qualification seats are forfeited" : "Cascading toggles will retain their individual expert values";
    body.innerHTML = `<div class="wizard-review"><div><p class="eyebrow">Review</p><h3>Your tournament at a glance</h3><p>Nothing changes until you apply this setup. You can still adjust every expert field before saving the tournament.</p></div><label class="wizard-scoring"><span>Scoring style</span><select data-wizard-setting="scoringStyle"><option value="keep" ${setupWizardDraft.scoringStyle === "keep" ? "selected" : ""}>Keep current points</option><option value="classic" ${setupWizardDraft.scoringStyle === "classic" ? "selected" : ""}>Classic · rewards the front</option><option value="balanced" ${setupWizardDraft.scoringStyle === "balanced" ? "selected" : ""}>Balanced · every place matters</option><option value="podium" ${setupWizardDraft.scoringStyle === "podium" ? "selected" : ""}>Podium-focused · top three matter most</option></select><small>${scoreLabels[setupWizardDraft.scoringStyle]}</small></label>${wizardStageMap(projection)}<div class="wizard-summary-grid"><article><small>Scale</small><strong>${projection.totalHeats} projected heats</strong><span>${projection.stagingHeats} staging + up to ${projection.championshipHeats} championship</span></article><article><small>Track load</small><strong>${projection.staging.size * setupWizardDraft.marblesPerRacer} marbles / staging heat</strong><span>${projection.staging.size} racers with ${setupWizardDraft.marblesPerRacer} marble${setupWizardDraft.marblesPerRacer === 1 ? "" : "s"} each</span></article><article><small>Final field</small><strong>Up to ${projection.finalists} racers</strong><span>The final always uses one marble per racer</span></article><article><small>Qualification seats</small><strong>${cascadingTitle}</strong><span>${cascadingDescription}</span></article></div></div>`;
  }
  footer.innerHTML = `<button type="button" class="secondary-button" data-wizard-back ${setupWizardStep === 0 ? "disabled" : ""}>Back</button><span>Step ${setupWizardStep + 1} of ${wizardSteps.length}</span>${setupWizardStep < wizardSteps.length - 1 ? `<button type="button" class="primary-button" data-wizard-next ${!projection.valid ? "disabled" : ""}>Continue <span aria-hidden="true">→</span></button>` : `<button type="button" class="primary-button" data-wizard-apply>Apply this setup</button>`}`;
}

function openSetupWizard() {
  setupWizardStep = 0;
  setupWizardDraft = setupWizardValuesFromForm();
  const dialog = document.querySelector("#setup-wizard");
  dialog.showModal();
  renderSetupWizardStep();
}

function wizardScoringPoints(style, placeCount) {
  if (style === "classic") return Array.from({length:placeCount}, (_, index) => [10, 7, 5, 3, 2, 1][index] || 0);
  if (style === "balanced") return Array.from({length:placeCount}, (_, index) => placeCount - index);
  if (style === "podium") return Array.from({length:placeCount}, (_, index) => [10, 6, 3][index] || 0);
  return null;
}

function applySetupWizard() {
  const form = document.querySelector("#config-form");
  const numericSettings = ["days", "heatsPerRacerPerDay", "maxMarblesPerHeat", "marblesPerRacer", "wildcardMaxMarblesPerHeat", "preliminaryMaxMarblesPerHeat", "maxFinalByeMarblesPerRacer", "maxPrelimMarblesForRacerWithFinalBye", "maxWildcardMarblesForRacerWithFinalBye", "maxPrelimPromotionMarblesPerRacer", "maxWildcardMarblesForRacerWithPrelimPromotion", "maxWildcardPromotionMarblesPerRacer", "wildcardRacersPromotedPerHeat", "preliminaryRacersPromotedPerHeat", "maxFinalRacers"];
  const checkboxSettings = ["allowCascadingFinalByeSelection", "allowCascadingPrelimPromotionSelection", "allowCascadingWildcardPromotionSelection"];
  numericSettings.forEach((name) => { form.elements[name].value = setupWizardDraft[name]; });
  checkboxSettings.forEach((name) => { form.elements[name].checked = setupWizardDraft[name]; });
  const projection = setupWizardProjection();
  const points = wizardScoringPoints(setupWizardDraft.scoringStyle, projection.staging.size * setupWizardDraft.marblesPerRacer);
  if (points) form.elements.points.value = points.join(", ");
  document.querySelector("#setup-wizard").close();
  updateSchedulePreview();
  notify("Wizard setup applied. Review the expert settings, then save the tournament.");
}

function renderSetup() {
  const c = state.competition;
  return `${viewHeader("Tournament settings", "Configure this tournament", "Every tournament keeps its own format, racers, heat results, standings, and final.")}
    <form id="config-form" class="setup-grid">
      <section class="panel config-panel"><div class="section-title"><span>01</span><div><h2>Tournament format</h2><p>Name the event and define its schedule.</p></div></div>
        <div class="setup-assistant"><div><span aria-hidden="true">✦</span><div><strong>New to tournament setup?</strong><small>Build a format step by step and preview how each choice affects the rounds and championship stages.</small></div></div><button type="button" class="primary-button" data-setup-wizard>Setup Wizard <span aria-hidden="true">→</span></button></div>
        <label class="field wide"><span>Tournament name</span><input name="name" value="${escapeHtml(c.name)}" maxlength="80" required></label>
        <details class="config-group" id="staging-rounds-group">
          <summary class="eyebrow">Staging rounds</summary>
          <div class="field-grid">
            <label class="field"><span class="field-label-text">Race rounds${fieldHelp("How many staging rounds run before the championship stage. Each round is its own self-contained pool: the bye/preliminary/wildcard tiers award one seat (or up to two, for wildcard) per round, so more rounds means more separate chances for every racer to qualify, and more marbles the top performers can stack up across rounds via the cross-round bonus caps.")}</span><input name="days" type="number" min="1" max="30" value="${c.days}" required></label>
            <label class="field"><span class="field-label-text">Heats per racer / round${fieldHelp("How many times each racer races within a single round. Together with racer count, &ldquo;Max marbles per heat&rdquo;, and &ldquo;Marbles per racer / heat&rdquo;, this decides how many heats a round splits into (see the preview below) and therefore how many total results feed that round's standings before byes/preliminary/wildcard seats are awarded.")}</span><input name="heatsPerRacerPerDay" type="number" min="1" max="20" value="${c.heatsPerRacerPerDay}" required></label>
            <label class="field"><span class="field-label-text">Max marbles per heat${fieldHelp("Sizes staging heats automatically: the app packs marbles into the largest complete heat that fits under this ceiling, then repeats heats until every racer has raced &ldquo;Heats per racer / round&rdquo; times. A lower limit means more, smaller heats per round. This is independent of the separate size limits for wildcard and preliminary heats further down.")}</span><input name="maxMarblesPerHeat" type="number" min="2" max="480" value="${c.maxMarblesPerHeat}" required></label>
            <label class="field"><span class="field-label-text">Marbles per racer / heat${fieldHelp("How many marbles each racer runs per staging heat. Applies to staging (round) heats only &mdash; the final always uses exactly one marble per racer regardless of this setting, and wildcard/preliminary heats instead use however many marble slots that racer earned through promotion.")}</span><input name="marblesPerRacer" type="number" min="1" max="20" value="${c.marblesPerRacer}" required></label>
          </div>
          <div class="schedule-preview" id="schedule-preview"><strong>${c.heatsPerDay} heats per round · ${c.racersPerHeat} racers per heat</strong><span>Every racer appears ${c.heatsPerRacerPerDay} times each round; each heat uses ${c.marblesPerHeat} of the ${c.maxMarblesPerHeat} allowed marbles.</span></div>
          <label class="field wide"><span class="field-label-text">Points by finishing place${fieldHelp("Comma-separated points awarded for 1st, 2nd, 3rd, etc. within a heat; any place beyond the list scores zero, and DNFs always score zero. These points sum across a round's heats into that round's standings, which decide who wins the round's bye/preliminary/wildcard seats &mdash; so changing the point spread can change who qualifies for the championship without changing a single finishing order.")}</span><input name="points" value="${state.points.join(", ")}" required></label>
        </details>
        <div class="config-callout" id="tiebreak-callout" hidden><span aria-hidden="true">&#9432;</span><div><strong>How ties are broken</strong><small>A round's standings are ranked by points, then wins, then the sum of each racer's literal finishing positions &mdash; so most ties resolve on their own from the actual results. If racers still match exactly on all three <em>and</em> the order between them would change who gets a bye, preliminary, or wildcard seat, that round pauses: racing can't continue until an organizer manually picks the order from a prompt on the Dashboard, Standings, or Rounds view. Ties that don't affect a seat (e.g. two racers tied for last) are left to resolve on their own by roster order, no prompt needed.</small></div></div>
        <details class="config-group" id="championship-round-group">
          <summary class="eyebrow">Championship round</summary>
          <div class="field-grid">
            <label class="field"><span class="field-label-text">Wildcard racers promoted / heat${fieldHelp("Top finishers from each <strong>wildcard heat</strong> who advance to the preliminary stage. This is separate from how a racer originally lands in a wildcard heat &mdash; that's decided by the &ldquo;Promotion to wildcard&rdquo; settings below, once per staging round.")}</span><input name="wildcardRacersPromotedPerHeat" type="number" min="1" max="24" value="${c.wildcardRacersPromotedPerHeat}" required></label>
            <label class="field"><span class="field-label-text">Preliminary racers promoted / heat${fieldHelp("Top finishers from each <strong>preliminary heat</strong> who advance to the final. Separate from how a racer originally reaches the preliminary stage &mdash; either a direct promotion from a staging round, or by placing well in a wildcard heat.")}</span><input name="preliminaryRacersPromotedPerHeat" type="number" min="1" max="24" value="${c.preliminaryRacersPromotedPerHeat}" required></label>
            <label class="field"><span class="field-label-text">Max Racers in Final${fieldHelp("Ceiling on the final's roster size. If final byes plus preliminary-heat qualifiers add up to more racers than this, the lowest-priority qualifiers are trimmed to fit.")}</span><input name="maxFinalRacers" type="number" min="2" max="24" value="${c.maxFinalRacers}" required></label>
          </div>
        </details>
        <div class="config-callout" id="tier-callout" hidden><span aria-hidden="true">&#9432;</span><div><strong>One tier per round</strong><small>A racer can hold at most one of bye, preliminary, or wildcard from any single staging round &mdash; 1st place is always reserved for the bye seat, and whoever actually wins bye/preliminary that round is excluded from the round's lower tiers even if a cascade moved that seat further down the standings. The three &ldquo;bonus&rdquo; marble caps below only grant <em>extra</em> marbles in a lower tier earned in a <em>different</em> round; they can never award a racer a second seat from the same round.</small></div></div>
        <details class="config-group">
          <summary class="eyebrow">Bye to final</summary>
          <div class="field-grid">
            <label class="field"><span class="field-label-text">Max final bye marbles per racer${fieldHelp("How many round wins (1st-place finishes) one racer may bank as bye marbles into the final, across the <strong>whole tournament</strong> &mdash; not per round. A racer who wins rounds 1 and 3 banks 2 bye marbles if this is 2 or higher; if capped at 1, their second win either cascades to that round's runner-up or is forfeited, depending on the cascading toggle below.")}</span><input name="maxFinalByeMarblesPerRacer" type="number" min="0" max="20" value="${c.maxFinalByeMarblesPerRacer}" required></label>
            <label class="field"><span class="field-label-text">Max prelim marbles for racer with final bye${fieldHelp("Extra preliminary marbles a racer may collect from <strong>other</strong> rounds once they already hold any bye marble, on top of (and instead of) the normal &ldquo;max prelim promotion marbles per racer&rdquo; cap, which doesn't apply to bye-tier racers. Never grants a preliminary seat from the same round as the bye &mdash; that round's 1st place is reserved for the bye tier only. Default 0 means bye-tier racers never also collect preliminary marbles.")}</span><input name="maxPrelimMarblesForRacerWithFinalBye" type="number" min="0" max="20" value="${c.maxPrelimMarblesForRacerWithFinalBye}" required></label>
            <label class="field"><span class="field-label-text">Max wildcard marbles for racer with final bye${fieldHelp("Same idea as the preliminary bonus above, but for the wildcard tier: extra wildcard marbles a bye-tier racer may collect from other rounds, never the same round as their bye. Default 0 disables it.")}</span><input name="maxWildcardMarblesForRacerWithFinalBye" type="number" min="0" max="20" value="${c.maxWildcardMarblesForRacerWithFinalBye}" required></label>
            <label class="field checkbox-field"><input name="allowCascadingFinalByeSelection" type="checkbox" ${c.allowCascadingFinalByeSelection ? "checked" : ""}><span class="field-label-text">Allow cascading final bye selection${fieldHelp("When a round's 1st-place finisher has already banked the max bye marbles above, this decides what happens to that round's bye seat. <strong>On:</strong> the seat cascades down the standings to the first racer still under their own cap. <strong>Off:</strong> the seat is simply forfeited for that round. Either way 1st place never receives a preliminary or wildcard seat instead &mdash; that position is reserved for the bye tier only.")}</span></label>
          </div>
        </details>
        <details class="config-group">
          <summary class="eyebrow">Promotion to preliminary</summary>
          <div class="field-grid">
            <label class="field"><span class="field-label-text">Max prelim promotion marbles per racer${fieldHelp("Caps how many preliminary marbles a racer with <strong>no</strong> bye marbles may hold across the tournament, earned by finishing 2nd (or the cascade target) in staging rounds. Bye-tier racers use the separate &ldquo;max prelim marbles for racer with final bye&rdquo; cap instead of this one.")}</span><input name="maxPrelimPromotionMarblesPerRacer" type="number" min="0" max="20" value="${c.maxPrelimPromotionMarblesPerRacer}" required></label>
            <label class="field"><span class="field-label-text">Max wildcard marbles for racer with prelim promotion${fieldHelp("Extra wildcard marbles a racer may collect from <strong>other</strong> rounds once they already hold any preliminary marble, on top of the normal wildcard cap, which doesn't apply to prelim-tier racers. Never grants a wildcard seat from the same round as the preliminary promotion. Default 0 disables it.")}</span><input name="maxWildcardMarblesForRacerWithPrelimPromotion" type="number" min="0" max="20" value="${c.maxWildcardMarblesForRacerWithPrelimPromotion}" required></label>
            <label class="field"><span class="field-label-text">Max marbles per preliminary heat${fieldHelp("Sizes preliminary heats automatically &mdash; the app packs qualifying marbles into as few full heats as possible under this ceiling. Doesn't affect who qualifies.")}</span><input name="preliminaryMaxMarblesPerHeat" type="number" min="2" max="480" value="${c.preliminaryMaxMarblesPerHeat}" required></label>
            <label class="field checkbox-field"><input name="allowCascadingPrelimPromotionSelection" type="checkbox" ${c.allowCascadingPrelimPromotionSelection ? "checked" : ""}><span class="field-label-text">Allow cascading prelim promotion selection${fieldHelp("When a round's 2nd place is already at their preliminary cap (or is that round's bye winner), this decides whether the seat cascades further down the standings to the next eligible racer (on) or is forfeited for that round (off).")}</span></label>
          </div>
        </details>
        <details class="config-group">
          <summary class="eyebrow">Promotion to wildcard</summary>
          <div class="field-grid">
            <label class="field"><span class="field-label-text">Max wildcard promotion marbles per racer${fieldHelp("Caps how many wildcard marbles a racer with <strong>no</strong> bye or preliminary marbles may hold across the tournament, earned from the two wildcard seats available in each staging round. Bye-tier and prelim-tier racers use their own bonus caps above instead of this one.")}</span><input name="maxWildcardPromotionMarblesPerRacer" type="number" min="0" max="20" value="${c.maxWildcardPromotionMarblesPerRacer}" required></label>
            <label class="field"><span class="field-label-text">Max marbles per wildcard heat${fieldHelp("Sizes wildcard heats automatically, the same way the preliminary heat-size limit does. Doesn't affect who qualifies.")}</span><input name="wildcardMaxMarblesPerHeat" type="number" min="2" max="480" value="${c.wildcardMaxMarblesPerHeat}" required></label>
            <label class="field checkbox-field"><input name="allowCascadingWildcardPromotionSelection" type="checkbox" ${c.allowCascadingWildcardPromotionSelection ? "checked" : ""}><span class="field-label-text">Allow cascading wildcard promotion selection${fieldHelp("When one of a round's two wildcard seats lands on a racer who's already at their wildcard cap (or who already holds a bye/preliminary seat from that same round), this decides whether the seat cascades to the next eligible finisher (on) or is forfeited for that round (off).")}</span></label>
          </div>
        </details>
      </section>
      <section class="panel config-panel"><div class="section-title"><span>02</span><div><h2>Racers</h2><p>Names and colors are used throughout the race sheets.</p></div></div><div id="contestant-list">${state.contestants.map(contestantRow).join("")}</div><button type="button" class="secondary-button full" data-add-contestant>+ Add racer</button></section>
      <section class="panel tournament-management"><div><p class="eyebrow">Tournament library</p><h2>Manage this tournament</h2><p>Create another tournament from the selector above, or permanently remove this one.</p></div><button type="button" class="danger-button" data-delete-tournament>Delete tournament</button></section>
      <div class="save-bar"><div><strong>Ready to update?</strong><span>Only ${escapeHtml(c.name)} is changed; other tournaments stay untouched.</span></div><button type="submit" class="primary-button">Save tournament</button></div>
    </form>
    <dialog id="setup-wizard" class="setup-wizard" aria-labelledby="setup-wizard-title"><div class="setup-wizard-shell"><header><div><p class="eyebrow">Guided tournament setup</p><h2 id="setup-wizard-title">Setup Wizard</h2></div><button type="button" data-close-setup-wizard aria-label="Close setup wizard">×</button></header><nav class="wizard-steps" aria-label="Setup progress">${wizardSteps.map((step, index) => `<span class="wizard-step"><b>${index + 1}</b>${step}</span>`).join("")}</nav><section id="setup-wizard-body" class="setup-wizard-body"></section><footer id="setup-wizard-footer" class="setup-wizard-footer"></footer></div></dialog>`;
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
  return {name:formData.get("name"), days:formData.get("days"), heatsPerRacerPerDay:formData.get("heatsPerRacerPerDay"), maxMarblesPerHeat:formData.get("maxMarblesPerHeat"), marblesPerRacer:formData.get("marblesPerRacer"), wildcardMaxMarblesPerHeat:formData.get("wildcardMaxMarblesPerHeat"), preliminaryMaxMarblesPerHeat:formData.get("preliminaryMaxMarblesPerHeat"), maxFinalByeMarblesPerRacer:formData.get("maxFinalByeMarblesPerRacer"), maxPrelimMarblesForRacerWithFinalBye:formData.get("maxPrelimMarblesForRacerWithFinalBye"), maxWildcardMarblesForRacerWithFinalBye:formData.get("maxWildcardMarblesForRacerWithFinalBye"), allowCascadingFinalByeSelection:formData.get("allowCascadingFinalByeSelection") === "on", maxPrelimPromotionMarblesPerRacer:formData.get("maxPrelimPromotionMarblesPerRacer"), allowCascadingPrelimPromotionSelection:formData.get("allowCascadingPrelimPromotionSelection") === "on", maxWildcardMarblesForRacerWithPrelimPromotion:formData.get("maxWildcardMarblesForRacerWithPrelimPromotion"), maxWildcardPromotionMarblesPerRacer:formData.get("maxWildcardPromotionMarblesPerRacer"), allowCascadingWildcardPromotionSelection:formData.get("allowCascadingWildcardPromotionSelection") === "on", wildcardRacersPromotedPerHeat:formData.get("wildcardRacersPromotedPerHeat"), preliminaryRacersPromotedPerHeat:formData.get("preliminaryRacersPromotedPerHeat"), maxFinalRacers:formData.get("maxFinalRacers"), points:String(formData.get("points")).split(",").map((value) => value.trim()), contestants, confirmReset};
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

const closeFieldHelpPopovers = () => {
  document.querySelectorAll(".field-help-popover:not([hidden]), .tie-popover:not([hidden])").forEach((popover) => { popover.hidden = true; });
  document.querySelectorAll('[data-help-toggle][aria-expanded="true"], [data-tie-toggle][aria-expanded="true"]').forEach((button) => button.setAttribute("aria-expanded", "false"));
};

document.addEventListener("click", (event) => {
  const helpToggle = event.target.closest("[data-help-toggle]");
  if (helpToggle) {
    const popover = document.getElementById(helpToggle.dataset.helpToggle);
    const isOpen = popover && !popover.hidden;
    closeFieldHelpPopovers();
    if (popover && !isOpen) { popover.hidden = false; helpToggle.setAttribute("aria-expanded", "true"); }
    return;
  }
  const tieToggle = event.target.closest("[data-tie-toggle]");
  if (tieToggle) {
    const popover = document.getElementById(tieToggle.dataset.tieToggle);
    const isOpen = popover && !popover.hidden;
    closeFieldHelpPopovers();
    if (popover && !isOpen) {
      popover.hidden = false;
      tieToggle.setAttribute("aria-expanded", "true");
      const anchor = tieToggle.getBoundingClientRect();
      const width = popover.offsetWidth;
      const left = Math.min(Math.max(12, anchor.left), window.innerWidth - width - 12);
      popover.style.left = `${left}px`;
      popover.style.top = `${Math.min(anchor.bottom + 8, window.innerHeight - popover.offsetHeight - 12)}px`;
    }
    return;
  }
  if (!event.target.closest(".field-help-popover") && !event.target.closest(".tie-popover")) closeFieldHelpPopovers();
  if (event.target.closest("[data-setup-wizard]")) { openSetupWizard(); return; }
  if (event.target.closest("[data-close-setup-wizard]")) { document.querySelector("#setup-wizard")?.close(); return; }
  const wizardPresetButton = event.target.closest("[data-wizard-preset]");
  if (wizardPresetButton) { setupWizardDraft = wizardPreset(wizardPresetButton.dataset.wizardPreset); renderSetupWizardStep(); return; }
  if (event.target.closest("[data-wizard-back]")) { setupWizardStep = Math.max(0, setupWizardStep - 1); renderSetupWizardStep(); return; }
  if (event.target.closest("[data-wizard-next]")) { setupWizardStep = Math.min(wizardSteps.length - 1, setupWizardStep + 1); renderSetupWizardStep(); return; }
  if (event.target.closest("[data-wizard-apply]")) { applySetupWizard(); return; }
  if (event.target.closest("[data-enter-kiosk]")) { setKioskMode(true, true); return; }
  if (event.target.closest("[data-exit-kiosk]")) { setKioskMode(false); return; }
  const standingsTierViewButton = event.target.closest("[data-standings-tier-view]");
  if (standingsTierViewButton) { standingsTierView = standingsTierViewButton.dataset.standingsTierView; render(); return; }
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
  if (event.target.closest("[data-open-tie-break]")) { syncTieBreakDialog(); tieBreakDialog.showModal(); return; }
  if (event.target.closest("[data-close-tie-break]")) { tieBreakDialog.close(); return; }
  const tiePickButton = event.target.closest("[data-tie-pick]");
  if (tiePickButton) { tieBreakOrder = [...tieBreakOrder, Number(tiePickButton.dataset.tiePick)]; syncTieBreakDialog(); return; }
  if (event.target.closest("[data-tie-break-reset]")) { tieBreakOrder = []; syncTieBreakDialog(); return; }
  if (event.target.closest("[data-tie-break-confirm]")) { confirmTieBreak(); return; }
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
  if (event.target.matches('[data-wizard-setting][type="number"]')) {
    const number = Number(event.target.value);
    if (event.target.value !== "" && Number.isFinite(number)) {
      setupWizardDraft[event.target.dataset.wizardSetting] = number;
      setupWizardDraft.preset = "custom";
      refreshSetupWizardFeedback();
    }
    return;
  }
  if (event.target.closest("#config-form") && ["days", "heatsPerRacerPerDay", "maxMarblesPerHeat", "marblesPerRacer"].includes(event.target.name)) updateSchedulePreview();
  if (event.target.matches('.contestant-color-marble input[type="color"]')) event.target.previousElementSibling?.style.setProperty("--marble-color", event.target.value);
});

document.addEventListener("change", (event) => {
  if (event.target.matches("[data-wizard-setting]")) {
    const setting = event.target.dataset.wizardSetting;
    setupWizardDraft[setting] = event.target.type === "number"
      ? boundedNumber(event.target.value, Number(event.target.min), Number(event.target.max), setupWizardDraft[setting])
      : event.target.value;
    if (setting !== "scoringStyle") setupWizardDraft.preset = "custom";
    renderSetupWizardStep();
    return;
  }
  if (event.target.matches("[data-wizard-repeat]")) {
    const cascade = event.target.value === "spread";
    setupWizardDraft.allowCascadingFinalByeSelection = cascade;
    setupWizardDraft.allowCascadingPrelimPromotionSelection = cascade;
    setupWizardDraft.allowCascadingWildcardPromotionSelection = cascade;
    setupWizardDraft.preset = "custom";
    renderSetupWizardStep();
  }
});

document.addEventListener("fullscreenchange", () => {
  if (kioskMode && kioskUsesBrowserFullscreen && !document.fullscreenElement) setKioskMode(false);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeFieldHelpPopovers();
});

// "toggle" doesn't bubble, so this has to listen during the capture phase.
document.addEventListener("toggle", (event) => {
  const calloutId = {"championship-round-group": "tier-callout", "staging-rounds-group": "tiebreak-callout"}[event.target.id];
  if (!calloutId) return;
  const callout = document.querySelector(`#${calloutId}`);
  if (callout) callout.hidden = !event.target.open;
}, true);

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
