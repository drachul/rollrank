from __future__ import annotations

import os
import tempfile
import unittest
from collections import Counter
from itertools import combinations


TEST_DATA = tempfile.TemporaryDirectory()
os.environ["APP_DATA_DIR"] = TEST_DATA.name

from app import app  # noqa: E402
from db import balanced_schedule, championship_field, connect  # noqa: E402
from report import final_finish_label, heat_finish_label  # noqa: E402


def start_heat(client, heat):
    if heat.get("stage", "staging") != "staging":
        return None
    return client.put(f'/api/heats/{heat["id"]}/start')


def score_heat_sequentially(client, heat):
    start_heat(client, heat)
    results = []
    position = 0
    for entry in heat["entries"]:
        for race_marble in entry["marbles"]:
            position += 1
            results.append(
                {
                    "contestantId": entry["contestantId"],
                    "marbleNumber": race_marble["number"],
                    "finish": position,
                }
            )
    return client.put(f'/api/heats/{heat["id"]}/results', json={"results": results})


def score_all_heats_sequentially(client, heats):
    state = None
    for heat in heats:
        response = score_heat_sequentially(client, heat)
        assert response.status_code == 200, response.get_json()
        state = response.get_json()
    return state


class MarbleRaceApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        client = app.test_client()
        response = client.get("/api/tournaments")
        cls.initial_tournaments = response.get_json()["tournaments"]
        response.close()
        created = client.post(
            "/api/tournaments", json={"name": "The Great Marble Race"}
        )
        if created.status_code != 201:
            raise RuntimeError("Could not create the shared tournament test fixture.")
        created.close()

    @classmethod
    def tearDownClass(cls) -> None:
        TEST_DATA.cleanup()

    def setUp(self) -> None:
        self.client = app.test_client()

    def test_seeded_state_has_schedule_and_standings(self) -> None:
        response = self.client.get("/api/state")
        self.assertEqual(response.status_code, 200)
        state = response.get_json()
        self.assertEqual(state["competition"]["name"], "The Great Marble Race")
        self.assertEqual(state["competition"]["id"], 1)
        self.assertEqual(len(state["tournaments"]), 1)
        self.assertEqual(len(state["days"]), 3)
        self.assertEqual(state["competition"]["heatsPerRacerPerDay"], 3)
        self.assertEqual(state["competition"]["maxMarblesPerHeat"], 6)
        self.assertEqual(state["competition"]["racersPerHeat"], 6)
        self.assertEqual(state["competition"]["marblesPerHeat"], 6)
        self.assertEqual(state["competition"]["marblesPerRacer"], 1)
        self.assertEqual(len(state["days"][0]["heats"]), 4)
        self.assertTrue(
            all(
                len(entry["marbles"]) == 1
                for entry in state["days"][0]["heats"][0]["entries"]
            )
        )
        self.assertEqual(len(state["standings"]), 8)
        for stage in ("wildcard", "preliminary"):
            self.assertFalse(state["championship"][stage]["ready"])
            self.assertEqual(state["championship"][stage]["heats"], [])
        self.assertFalse(state["championship"]["final"]["ready"])
        self.assertIsNone(state["championship"]["final"]["heat"])

    def test_new_database_starts_without_a_tournament(self) -> None:
        self.assertEqual(self.initial_tournaments, [])

    def test_rollrank_landing_and_workspace_routes(self) -> None:
        landing = self.client.get("/")
        workspace = self.client.get("/workspace")
        self.assertEqual(landing.status_code, 200)
        self.assertEqual(workspace.status_code, 200)
        html = landing.get_data(as_text=True)
        self.assertIn("RollRank", html)
        self.assertIn("Tournament hub", html)
        self.assertIn('id="tournament-index-list"', html)
        self.assertIn("+ New tournament", html)
        self.assertNotIn("Run the race.", html)
        self.assertNotIn('/static/rollrank-hero.png', html)
        self.assertIn('id="workspace-shell" hidden', html)
        landing.close()
        workspace.close()

    def test_schedule_balances_appearances_and_opponents(self) -> None:
        state = self.client.get("/api/state").get_json()
        target = state["competition"]["heatsPerRacerPerDay"]
        pair_counts = Counter()
        for day in state["days"]:
            appearances = Counter()
            for heat in day["heats"]:
                racer_ids = [entry["contestantId"] for entry in heat["entries"]]
                appearances.update(racer_ids)
                pair_counts.update(tuple(sorted(pair)) for pair in combinations(racer_ids, 2))
            self.assertTrue(all(value == target for value in appearances.values()))
            self.assertEqual(len(appearances), len(state["contestants"]))
        self.assertLessEqual(max(pair_counts.values()) - min(pair_counts.values()), 2)

    def test_schedule_reaches_even_pairing_when_configuration_allows_it(self) -> None:
        schedule = balanced_schedule(list(range(1, 8)), 4, 4, 3, 42)
        pair_counts = Counter()
        for day in schedule:
            appearances = Counter(racer_id for heat in day for racer_id in heat)
            self.assertEqual([appearances[racer_id] for racer_id in range(1, 8)], [4] * 7)
            for heat in day:
                pair_counts.update(tuple(sorted(pair)) for pair in combinations(heat, 2))
        self.assertEqual(
            {pair_counts[pair] for pair in combinations(range(1, 8), 2)},
            {6},
        )

    def test_complete_heat_updates_points(self) -> None:
        state = self.client.get("/api/state").get_json()
        heat = state["days"][0]["heats"][0]
        results = []
        position = 0
        for entry in heat["entries"]:
            for race_marble in entry["marbles"]:
                position += 1
                results.append(
                    {
                        "contestantId": entry["contestantId"],
                        "marbleNumber": race_marble["number"],
                        "finish": position,
                    }
                )
        start_heat(self.client, heat)
        response = self.client.put(f'/api/heats/{heat["id"]}/results', json={"results": results})
        self.assertEqual(response.status_code, 200)
        updated = response.get_json()
        self.assertEqual(updated["competition"]["completedHeats"], 1)
        self.assertTrue(updated["days"][0]["heats"][0]["complete"])
        summaries = self.client.get("/api/tournaments").get_json()["tournaments"]
        self.assertEqual(len(summaries), 1)
        self.assertIsNotNone(summaries[0]["leader"])
        self.assertEqual(
            summaries[0]["leader"]["totalPoints"],
            max(racer["totalPoints"] for racer in updated["standings"]),
        )

    def test_heat_dnf_scores_zero_and_completes_heat(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "DNF Test Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        heat = created["days"][0]["heats"][0]
        results = []
        next_finish = 1
        for index, entry in enumerate(heat["entries"]):
            for race_marble in entry["marbles"]:
                finish = 0 if index < 2 else next_finish
                if finish:
                    next_finish += 1
                results.append(
                    {
                        "contestantId": entry["contestantId"],
                        "marbleNumber": race_marble["number"],
                        "finish": finish,
                    }
                )

        start_heat(self.client, heat)
        response = self.client.put(
            f'/api/heats/{heat["id"]}/results', json={"results": results}
        )
        self.assertEqual(response.status_code, 200)
        updated = response.get_json()
        updated_heat = next(
            item
            for day in updated["days"]
            for item in day["heats"]
            if item["id"] == heat["id"]
        )
        self.assertTrue(updated_heat["complete"])
        dnf_marbles = [
            race_marble
            for entry in updated_heat["entries"]
            for race_marble in entry["marbles"]
            if race_marble["finish"] == 0
        ]
        self.assertEqual(len(dnf_marbles), 2)
        self.assertTrue(all(race_marble["points"] == 0 for race_marble in dnf_marbles))
        self.assertEqual(heat_finish_label(0), "DNF")
        self.assertEqual(heat_finish_label(2), "2nd")

    def test_pdf_endpoint(self) -> None:
        tournament_id = self.client.get("/api/state").get_json()["competition"]["id"]
        response = self.client.get(f"/api/tournaments/{tournament_id}/report.pdf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertTrue(response.data.startswith(b"%PDF"))
        self.assertGreater(len(response.data), 5000)

    def test_final_labels_and_podium_are_in_frontend(self) -> None:
        index_response = self.client.get("/")
        frontend_response = self.client.get("/static/app.js")
        styles_response = self.client.get("/static/styles.css")
        index = index_response.get_data(as_text=True)
        frontend = frontend_response.get_data(as_text=True)
        styles = styles_response.get_data(as_text=True)
        index_response.close()
        frontend_response.close()
        styles_response.close()
        self.assertIn('<button data-view="heats">Rounds</button>', index)
        self.assertNotIn('data-view="championship"', index)
        self.assertIn('rel="icon" type="image/png" href="/static/marble-logo.png"', index)
        self.assertIn('class="brand-mark" src="/static/marble-logo.png"', index)
        self.assertIn('class="brand-wordmark"><strong>RollRank</strong>', index)
        self.assertIn('RollRank · Marble tournament control', index)
        self.assertIn('class="pdf-icon" aria-hidden="true"></span>', index)
        self.assertIn('/static/fontawesome-file-pdf.svg', styles)
        self.assertNotIn("↗", index)
        self.assertIn('id="tournament-switcher"', index)
        self.assertNotIn('id="race-title"', index)
        self.assertNotIn("raceTitle", frontend)
        self.assertIn('document.title = `${state.competition.name} · RollRank`', frontend)
        self.assertIn("Live leader", frontend)
        self.assertIn("Open dashboard", frontend)
        self.assertIn("No tournaments yet", frontend)
        self.assertIn('class="new-tournament-label">New</span>', index)
        self.assertIn("Create a fresh tournament", index)
        self.assertIn("Max final racers", frontend)
        self.assertIn("Max marbles per championship heat", frontend)
        self.assertIn("Max bye marbles per racer", frontend)
        self.assertIn("Top 3 podium", frontend)
        self.assertIn("Gold trophy", frontend)
        self.assertIn("Silver trophy", frontend)
        self.assertIn("Bronze trophy", frontend)
        self.assertIn("Sad face", frontend)
        self.assertIn("mobile-standings", frontend)
        self.assertIn("const form = event.target;", frontend)
        self.assertNotIn("const form = event.currentTarget;", frontend)
        self.assertIn("Race rounds", frontend)
        self.assertIn("Heats per racer / round", frontend)
        self.assertIn("Marbles per racer / heat", frontend)
        self.assertIn("Max marbles per heat", frontend)
        self.assertNotIn("<span>Racers per heat</span>", frontend)
        self.assertIn("data-marble-number", frontend)
        self.assertIn("DNF · 0 pts", frontend)
        self.assertIn('viewHeader("Results desk", "Rounds"', frontend)
        self.assertIn('data-day="championship"', frontend)
        self.assertNotIn("Enter heat results", frontend)
        self.assertNotIn("Review completed heats", frontend)
        self.assertNotIn("Edit tournament", frontend)
        self.assertNotIn(">Print report</a>", frontend)
        self.assertIn("Tournament standings", frontend)
        self.assertNotIn("Championship standings", frontend)
        self.assertNotIn("Race days", frontend)
        self.assertNotIn("Heats per racer / day", frontend)
        self.assertIn("@media (max-width:370px)", styles)
        self.assertIn("grid-template-columns:repeat(5,1fr)", styles)
        self.assertIn("padding:10px 14px; backdrop-filter:none", styles)
        self.assertIn("top:auto; right:0; bottom:0", styles)
        self.assertIn("align-self:auto", styles)
        self.assertIn("flex:0 0 34px", styles)
        self.assertIn("grid-template-columns:minmax(0,1fr) 42px", styles)
        self.assertIn('url("/static/rollrank-hero.png") center/cover fixed', styles)
        self.assertIn("background:rgba(5,21,34,.92)", styles)
        self.assertIn(".workspace-mode .view-heading h1 { color:#fff; }", styles)
        self.assertIn("data-enter-kiosk", frontend)
        self.assertIn("data-exit-kiosk", frontend)
        self.assertIn("function renderKioskDashboard()", frontend)
        self.assertIn("window.setInterval(refreshKioskState, 5000)", frontend)
        self.assertIn('url.searchParams.set("display", "kiosk")', frontend)
        self.assertIn(".workspace-mode.kiosk-mode", styles)
        self.assertIn(".kiosk-standing-list", styles)
        self.assertIn("function currentTournamentStatus()", frontend)
        self.assertIn('class="status-chip ${status.className}"', frontend)
        self.assertNotIn("Tournament in progress", frontend)
        self.assertIn(".status-chip.final-ready", styles)
        self.assertIn(".status-chip.complete", styles)
        self.assertIn("function renderKioskFinalDashboard()", frontend)
        self.assertIn("function renderDashboardFinalSummary()", frontend)
        self.assertIn("if (state.championship.final.complete) return renderDashboardFinalSummary();", frontend)
        self.assertIn("function fireworksMarkup()", frontend)
        self.assertIn("if (championship.final.ready) return renderKioskFinalDashboard();", frontend)
        self.assertIn("dashboard-final-racers", frontend)
        self.assertIn("kiosk-final-racers", frontend)
        self.assertIn("kiosk-final-podium", frontend)
        self.assertIn("Awaiting result", frontend)
        self.assertIn("kiosk-fireworks", frontend)
        self.assertIn("function finalDnfPlace", frontend)
        self.assertIn("Number(racer.finish) > 0).length + 1", frontend)
        self.assertIn("finalFinishLabel", frontend)
        self.assertIn("dnfs.length", frontend)
        self.assertIn(".kiosk-final-dashboard", styles)
        self.assertIn(".dashboard-final-summary", styles)
        self.assertIn(".dashboard-final-racers", styles)
        self.assertIn("@keyframes kiosk-firework-burst", styles)
        self.assertIn(".kiosk-fireworks { display:none; }", styles)
        self.assertIn("no-cache", index_response.headers["Cache-Control"])
        # Championship bracket rewrite: three-stage rendering, no leftover
        # single-final editor.
        self.assertIn("function originBadge(entry)", frontend)
        self.assertIn(".origin-badge", styles)
        self.assertIn("championship-stage", frontend)
        self.assertNotIn("saveChampionship", frontend)
        self.assertNotIn("data-champ-result", frontend)
        self.assertNotIn("function finalResultOptions", frontend)

    def test_full_championship_workflow(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Full Bracket Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        staging_heats = [heat for day in created["days"] for heat in day["heats"]]
        state = score_all_heats_sequentially(self.client, staging_heats)
        self.assertTrue(state["championship"]["wildcard"]["ready"])

        if state["championship"]["wildcard"]["heats"]:
            state = score_all_heats_sequentially(self.client, state["championship"]["wildcard"]["heats"])
        self.assertTrue(state["championship"]["preliminary"]["ready"])

        if state["championship"]["preliminary"]["heats"]:
            state = score_all_heats_sequentially(self.client, state["championship"]["preliminary"]["heats"])
        self.assertTrue(state["championship"]["final"]["ready"])

        final_heat = state["championship"]["final"]["heat"]
        self.assertIsNotNone(final_heat)
        entries = final_heat["entries"]
        self.assertGreaterEqual(len(entries), 3)

        no_winner_results = [
            {
                "contestantId": entry["contestantId"],
                "marbleNumber": entry["marbles"][0]["number"],
                "finish": 0,
            }
            for entry in entries
        ]
        rejected = self.client.put(
            f'/api/heats/{final_heat["id"]}/results', json={"results": no_winner_results}
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("first-place", rejected.get_json()["error"])

        finisher_count = len(entries) - 2
        final_results = [
            {
                "contestantId": entry["contestantId"],
                "marbleNumber": entry["marbles"][0]["number"],
                "finish": index + 1 if index < finisher_count else 0,
            }
            for index, entry in enumerate(entries)
        ]
        response = self.client.put(
            f'/api/heats/{final_heat["id"]}/results', json={"results": final_results}
        )
        self.assertEqual(response.status_code, 200)
        finished = response.get_json()
        self.assertTrue(finished["championship"]["final"]["complete"])
        self.assertEqual(finished["championship"]["final"]["champion"]["finish"], 1)
        dnf_entries = [
            entry for entry in finished["championship"]["final"]["heat"]["entries"] if entry["finish"] == 0
        ]
        self.assertEqual(len(dnf_entries), 2)
        tied_last_place = finisher_count + 1
        tied_last_label = final_finish_label(0, tied_last_place)
        self.assertTrue(tied_last_label.startswith(f"T-{tied_last_place}"))
        self.assertTrue(tied_last_label.endswith(" DNF"))
        self.assertEqual(final_finish_label(0, 3), "T-3rd DNF")

    def test_multiple_marbles_per_racer_sum_heat_points(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Double Marble Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        racers = [
            {"name": racer["name"], "color": racer["color"]}
            for racer in created["contestants"][:6]
        ]
        response = self.client.put(
            f"/api/tournaments/{tournament_id}",
            json={
                "name": "Double Marble Cup",
                "days": 1,
                "heatsPerRacerPerDay": 1,
                "maxMarblesPerHeat": 6,
                "marblesPerRacer": 2,
                "championshipMaxMarblesPerHeat": 6,
                "maxByeMarblesPerRacer": 1,
                "maxFinalRacers": 3,
                "points": [10, 7, 5, 3, 2, 1],
                "contestants": racers,
            },
        )
        self.assertEqual(response.status_code, 200)
        state = response.get_json()
        self.assertEqual(state["competition"]["marblesPerRacer"], 2)
        self.assertEqual(state["competition"]["maxMarblesPerHeat"], 6)
        self.assertEqual(state["competition"]["racersPerHeat"], 3)
        self.assertEqual(state["competition"]["marblesPerHeat"], 6)
        self.assertEqual(len(state["points"]), 6)

        for heat_index, heat in enumerate(state["days"][0]["heats"]):
            self.assertTrue(all(len(entry["marbles"]) == 2 for entry in heat["entries"]))
            results = []
            finish = 0
            for entry in heat["entries"]:
                for race_marble in entry["marbles"]:
                    finish += 1
                    results.append(
                        {
                            "contestantId": entry["contestantId"],
                            "marbleNumber": race_marble["number"],
                            "finish": finish,
                        }
                    )
            start_heat(self.client, heat)
            scored = self.client.put(
                f'/api/heats/{heat["id"]}/results', json={"results": results}
            )
            self.assertEqual(scored.status_code, 200)
            state = scored.get_json()
            scored_heat = state["days"][0]["heats"][heat_index]
            self.assertEqual(
                [entry["points"] for entry in scored_heat["entries"]],
                [17, 8, 3],
            )

        self.assertTrue(state["championship"]["wildcard"]["ready"])
        self.assertEqual(
            sorted((racer["totalPoints"] for racer in state["standings"]), reverse=True),
            [17, 17, 8, 8, 3, 3],
        )
        wildcard_heats = state["championship"]["wildcard"]["heats"]
        self.assertTrue(wildcard_heats)
        self.assertTrue(
            all(len(entry["marbles"]) == 2 for heat in wildcard_heats for entry in heat["entries"])
        )

        # The final always races one marble per racer regardless of
        # marblesPerRacer, since champion/podium/DNF all key off a single
        # finish per racer.
        state = score_all_heats_sequentially(self.client, wildcard_heats)
        if state["championship"]["preliminary"]["heats"]:
            state = score_all_heats_sequentially(self.client, state["championship"]["preliminary"]["heats"])
        self.assertTrue(state["championship"]["final"]["ready"])
        final_heat = state["championship"]["final"]["heat"]
        self.assertTrue(all(len(entry["marbles"]) == 1 for entry in final_heat["entries"]))
        finished = score_heat_sequentially(self.client, final_heat).get_json()
        self.assertTrue(finished["championship"]["final"]["complete"])
        self.assertIsNotNone(finished["championship"]["final"]["champion"])
        self.assertEqual(finished["championship"]["final"]["champion"]["finish"], 1)

        deleted = self.client.delete(f"/api/tournaments/{tournament_id}")
        self.assertEqual(deleted.status_code, 200)

    def test_max_marbles_calculates_largest_complete_heat(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Calculated Heat Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        racers = [
            {"name": racer["name"], "color": racer["color"]}
            for racer in created["contestants"]
        ]
        response = self.client.put(
            f"/api/tournaments/{tournament_id}",
            json={
                "name": "Calculated Heat Cup",
                "days": 1,
                "heatsPerRacerPerDay": 3,
                "maxMarblesPerHeat": 10,
                "marblesPerRacer": 2,
                "championshipMaxMarblesPerHeat": 6,
                "maxByeMarblesPerRacer": 1,
                "maxFinalRacers": 6,
                "points": [10, 7, 5, 3, 2, 1, 0, 0],
                "contestants": racers,
            },
        )
        self.assertEqual(response.status_code, 200)
        state = response.get_json()
        self.assertEqual(state["competition"]["racersPerHeat"], 4)
        self.assertEqual(state["competition"]["marblesPerHeat"], 8)
        self.assertEqual(state["competition"]["maxMarblesPerHeat"], 10)
        self.assertEqual(state["competition"]["heatsPerDay"], 6)
        self.assertTrue(
            all(
                len(heat["entries"]) == 4
                and sum(len(entry["marbles"]) for entry in heat["entries"]) == 8
                for heat in state["days"][0]["heats"]
            )
        )
        deleted = self.client.delete(f"/api/tournaments/{tournament_id}")
        self.assertEqual(deleted.status_code, 200)

    def test_tournaments_keep_settings_and_results_independent(self) -> None:
        original = self.client.get("/api/state").get_json()
        original_id = original["competition"]["id"]
        original_completed = original["competition"]["completedHeats"]
        response = self.client.post("/api/tournaments", json={"name": "Sprint Cup"})
        self.assertEqual(response.status_code, 201)
        created = response.get_json()
        second_id = created["competition"]["id"]
        self.assertNotEqual(second_id, original_id)
        self.assertEqual(len(created["tournaments"]), 2)

        racers = [
            {"name": racer["name"], "color": racer["color"]}
            for racer in created["contestants"][:6]
        ]
        configured = self.client.put(
            f"/api/tournaments/{second_id}",
            json={
                "name": "Sprint Cup",
                "days": 2,
                "heatsPerRacerPerDay": 1,
                "maxMarblesPerHeat": 3,
                "marblesPerRacer": 1,
                "championshipMaxMarblesPerHeat": 6,
                "maxByeMarblesPerRacer": 1,
                "maxFinalRacers": 3,
                "points": [5, 3, 1],
                "contestants": racers,
            },
        )
        self.assertEqual(configured.status_code, 200)
        second = configured.get_json()
        self.assertEqual(second["competition"]["days"], 2)
        self.assertEqual(second["competition"]["totalHeats"], 4)
        heat = second["days"][0]["heats"][0]
        results = [
            {
                "contestantId": entry["contestantId"],
                "marbleNumber": entry["marbles"][0]["number"],
                "finish": position,
            }
            for position, entry in enumerate(heat["entries"], start=1)
        ]
        start_heat(self.client, heat)
        scored = self.client.put(
            f'/api/heats/{heat["id"]}/results', json={"results": results}
        ).get_json()
        self.assertEqual(scored["competition"]["id"], second_id)
        self.assertEqual(scored["competition"]["completedHeats"], 1)

        unchanged = self.client.get(f"/api/state?tournamentId={original_id}").get_json()
        self.assertEqual(unchanged["competition"]["name"], original["competition"]["name"])
        self.assertEqual(unchanged["competition"]["days"], original["competition"]["days"])
        self.assertEqual(unchanged["competition"]["completedHeats"], original_completed)

        deleted = self.client.delete(f"/api/tournaments/{second_id}")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(
            self.client.get(f"/api/state?tournamentId={second_id}").status_code,
            404,
        )

    def test_round_scoped_standings_and_bye_cap_forfeits_excess_wins(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Round Standings Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        contestant_ids = [racer["id"] for racer in created["contestants"]]
        configured = self.client.put(
            f"/api/tournaments/{tournament_id}",
            json={
                "name": "Round Standings Cup",
                "days": 3,
                "heatsPerRacerPerDay": 1,
                "maxMarblesPerHeat": 8,
                "marblesPerRacer": 1,
                "championshipMaxMarblesPerHeat": 6,
                "maxByeMarblesPerRacer": 1,
                "maxFinalRacers": 6,
                "points": [10, 7, 5, 3, 2, 1, 0, 0],
                "contestants": [
                    {"name": racer["name"], "color": racer["color"]}
                    for racer in created["contestants"]
                ],
            },
        ).get_json()
        self.assertEqual(configured["competition"]["heatsPerDay"], 1)

        # racer[0] wins every round; everyone else rotates through the
        # remaining places so a different racer holds each other rank each day.
        state = configured
        for day_index, day in enumerate(state["days"]):
            heat = day["heats"][0]
            results = []
            for entry in heat["entries"]:
                index = contestant_ids.index(entry["contestantId"])
                finish = 1 if index == 0 else 2 + ((index - 1 + day_index) % 7)
                results.append(
                    {
                        "contestantId": entry["contestantId"],
                        "marbleNumber": entry["marbles"][0]["number"],
                        "finish": finish,
                    }
                )
            start_heat(self.client, heat)
            saved = self.client.put(f'/api/heats/{heat["id"]}/results', json={"results": results})
            self.assertEqual(saved.status_code, 200)
            state = saved.get_json()

        connection = connect()
        try:
            field = championship_field(connection, tournament_id)
        finally:
            connection.close()

        winner_id = contestant_ids[0]
        winner_byes = [item for item in field["byes"] if item["racerId"] == winner_id]
        self.assertEqual(len(winner_byes), 1)
        self.assertEqual(winner_byes[0]["originRound"], 1)
        self.assertTrue(all(item["racerId"] != winner_id for item in field["preliminaryDirect"]))
        self.assertTrue(all(item["racerId"] != winner_id for item in field["wildcardPool"]))

    def test_wildcard_preliminary_interleaving_avoids_same_round_clustering(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Interleave Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        contestant_ids = [racer["id"] for racer in created["contestants"]]
        state = self.client.put(
            f"/api/tournaments/{tournament_id}",
            json={
                "name": "Interleave Cup",
                "days": 3,
                "heatsPerRacerPerDay": 1,
                "maxMarblesPerHeat": 8,
                "marblesPerRacer": 1,
                "championshipMaxMarblesPerHeat": 3,
                "maxByeMarblesPerRacer": 1,
                "maxFinalRacers": 6,
                "points": [10, 7, 5, 3, 2, 1, 0, 0],
                "contestants": [
                    {"name": racer["name"], "color": racer["color"]}
                    for racer in created["contestants"]
                ],
            },
        ).get_json()

        # racers[6]/[7] always take 1st/2nd; each round's 3rd/4th (the wildcard
        # pool) are two racers no other round places in the wildcard zone.
        finish_orders = [
            [6, 7, 0, 1, 2, 3, 4, 5],
            [6, 7, 2, 3, 0, 1, 4, 5],
            [6, 7, 4, 5, 0, 1, 2, 3],
        ]
        for day, order in zip(state["days"], finish_orders):
            heat = day["heats"][0]
            finishes = {
                contestant_ids[racer_index]: place
                for place, racer_index in enumerate(order, start=1)
            }
            results = [
                {
                    "contestantId": entry["contestantId"],
                    "marbleNumber": entry["marbles"][0]["number"],
                    "finish": finishes[entry["contestantId"]],
                }
                for entry in heat["entries"]
            ]
            start_heat(self.client, heat)
            saved = self.client.put(f'/api/heats/{heat["id"]}/results', json={"results": results})
            self.assertEqual(saved.status_code, 200)
            state = saved.get_json()

        wildcard = state["championship"]["wildcard"]
        self.assertTrue(wildcard["ready"])
        self.assertGreaterEqual(len(wildcard["heats"]), 2)
        for heat in wildcard["heats"]:
            origin_rounds = [entry["originRound"] for entry in heat["entries"]]
            self.assertEqual(len(origin_rounds), len(set(origin_rounds)))

    def test_cascade_reset_confirmation_required_then_rebuilds(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Cascade Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        first_heat_id = created["days"][0]["heats"][0]["id"]
        staging_heats = [heat for day in created["days"] for heat in day["heats"]]
        state = score_all_heats_sequentially(self.client, staging_heats)
        self.assertTrue(state["championship"]["wildcard"]["ready"])
        self.assertTrue(state["championship"]["wildcard"]["heats"])
        state = score_all_heats_sequentially(self.client, state["championship"]["wildcard"]["heats"])
        self.assertTrue(state["championship"]["wildcard"]["complete"])

        # Reversing the first staging heat's results changes that round's
        # standings, and therefore the wildcard field, which already has an
        # entered result -- this must be gated behind confirmReset.
        heat = next(h for day in state["days"] for h in day["heats"] if h["id"] == first_heat_id)
        entry_count = sum(len(entry["marbles"]) for entry in heat["entries"])
        position = entry_count
        reversed_results = []
        for entry in heat["entries"]:
            for race_marble in entry["marbles"]:
                reversed_results.append(
                    {
                        "contestantId": entry["contestantId"],
                        "marbleNumber": race_marble["number"],
                        "finish": position,
                    }
                )
                position -= 1

        blocked = self.client.put(
            f'/api/heats/{first_heat_id}/results', json={"results": reversed_results}
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertTrue(blocked.get_json()["requiresReset"])

        confirmed = self.client.put(
            f'/api/heats/{first_heat_id}/results',
            json={"results": reversed_results, "confirmReset": True},
        )
        self.assertEqual(confirmed.status_code, 200)

    def test_small_tournament_auto_advances_without_racing(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Tiny Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        racers = [
            {"name": racer["name"], "color": racer["color"]}
            for racer in created["contestants"][:2]
        ]
        configured = self.client.put(
            f"/api/tournaments/{tournament_id}",
            json={
                "name": "Tiny Cup",
                "days": 2,
                "heatsPerRacerPerDay": 1,
                "maxMarblesPerHeat": 2,
                "marblesPerRacer": 1,
                "championshipMaxMarblesPerHeat": 6,
                "maxByeMarblesPerRacer": 1,
                "maxFinalRacers": 2,
                "points": [10, 7],
                "contestants": racers,
            },
        ).get_json()
        self.assertEqual(configured["competition"]["racersPerHeat"], 2)
        racer_a, racer_b = (row["id"] for row in configured["contestants"])
        state = configured
        for day in state["days"]:
            heat = day["heats"][0]
            results = [
                {
                    "contestantId": entry["contestantId"],
                    "marbleNumber": entry["marbles"][0]["number"],
                    "finish": 1 if entry["contestantId"] == racer_a else 2,
                }
                for entry in heat["entries"]
            ]
            start_heat(self.client, heat)
            saved = self.client.put(f'/api/heats/{heat["id"]}/results', json={"results": results})
            self.assertEqual(saved.status_code, 200)
            state = saved.get_json()

        champ = state["championship"]
        self.assertTrue(champ["wildcard"]["ready"])
        self.assertTrue(champ["wildcard"]["skipped"])
        self.assertEqual(champ["wildcard"]["fieldSize"], 0)
        self.assertTrue(champ["preliminary"]["ready"])
        self.assertTrue(champ["preliminary"]["skipped"])
        self.assertEqual(champ["preliminary"]["fieldSize"], 1)
        self.assertTrue(champ["final"]["ready"])
        final_entries = champ["final"]["heat"]["entries"]
        origins = {entry["contestantId"]: entry["originStage"] for entry in final_entries}
        self.assertEqual(origins[racer_a], "bye")
        self.assertEqual(origins[racer_b], "stage-skip")

    def test_final_field_trims_to_max_final_racers(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Trim Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        contestant_ids = [racer["id"] for racer in created["contestants"]]
        state = self.client.put(
            f"/api/tournaments/{tournament_id}",
            json={
                "name": "Trim Cup",
                "days": 4,
                "heatsPerRacerPerDay": 1,
                "maxMarblesPerHeat": 8,
                "marblesPerRacer": 1,
                "championshipMaxMarblesPerHeat": 6,
                "maxByeMarblesPerRacer": 1,
                "maxFinalRacers": 3,
                "points": [10, 7, 5, 3, 2, 1, 0, 0],
                "contestants": [
                    {"name": racer["name"], "color": racer["color"]}
                    for racer in created["contestants"]
                ],
            },
        ).get_json()

        # Each round has its own distinct 1st/2nd place pair, so four rounds
        # bank four distinct bye winners -- more than maxFinalRacers=3 allows.
        pairs = [(0, 1), (2, 3), (4, 5), (6, 7)]
        for day_index, day in enumerate(state["days"]):
            heat = day["heats"][0]
            winner_index, second_index = pairs[day_index]
            remaining = [index for index in range(8) if index not in (winner_index, second_index)]
            finish_by_index = {winner_index: 1, second_index: 2}
            for rank, racer_index in enumerate(remaining, start=3):
                finish_by_index[racer_index] = rank
            results = [
                {
                    "contestantId": entry["contestantId"],
                    "marbleNumber": entry["marbles"][0]["number"],
                    "finish": finish_by_index[contestant_ids.index(entry["contestantId"])],
                }
                for entry in heat["entries"]
            ]
            start_heat(self.client, heat)
            saved = self.client.put(f'/api/heats/{heat["id"]}/results', json={"results": results})
            self.assertEqual(saved.status_code, 200)
            state = saved.get_json()

        self.assertTrue(state["championship"]["wildcard"]["ready"])
        if state["championship"]["wildcard"]["heats"]:
            state = score_all_heats_sequentially(self.client, state["championship"]["wildcard"]["heats"])
        self.assertTrue(state["championship"]["preliminary"]["ready"])
        if state["championship"]["preliminary"]["heats"]:
            state = score_all_heats_sequentially(self.client, state["championship"]["preliminary"]["heats"])

        final_stage = state["championship"]["final"]
        self.assertTrue(final_stage["ready"])
        # Four rounds each bank a distinct bye winner, so the pre-trim field
        # (at least 4 byes) exceeds maxFinalRacers=3 and must be trimmed down.
        self.assertGreater(final_stage["trimmedCount"], 0)
        self.assertEqual(len(final_stage["heat"]["entries"]), 3)
        self.assertLessEqual(final_stage["byeCount"], 3)

    def test_championship_config_validation(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Config Validation Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        racers = [
            {"name": racer["name"], "color": racer["color"]}
            for racer in created["contestants"]
        ]
        base_payload = {
            "name": "Config Validation Cup",
            "days": 3,
            "heatsPerRacerPerDay": 3,
            "maxMarblesPerHeat": 6,
            "marblesPerRacer": 1,
            "points": [10, 7, 5, 3, 2, 1],
            "contestants": racers,
        }

        missing_field = self.client.put(
            f"/api/tournaments/{tournament_id}",
            json={**base_payload, "maxByeMarblesPerRacer": 1, "maxFinalRacers": 6},
        )
        self.assertEqual(missing_field.status_code, 400)

        out_of_range = self.client.put(
            f"/api/tournaments/{tournament_id}",
            json={
                **base_payload,
                "championshipMaxMarblesPerHeat": 6,
                "maxByeMarblesPerRacer": -1,
                "maxFinalRacers": 6,
            },
        )
        self.assertEqual(out_of_range.status_code, 400)

        valid = self.client.put(
            f"/api/tournaments/{tournament_id}",
            json={
                **base_payload,
                "championshipMaxMarblesPerHeat": 4,
                "maxByeMarblesPerRacer": 2,
                "maxFinalRacers": 5,
            },
        )
        self.assertEqual(valid.status_code, 200)
        state = valid.get_json()
        self.assertEqual(state["competition"]["championshipMaxMarblesPerHeat"], 4)
        self.assertEqual(state["competition"]["maxByeMarblesPerRacer"], 2)
        self.assertEqual(state["competition"]["maxFinalRacers"], 5)

    def test_scoring_unstarted_staging_heat_is_rejected(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Unstarted Heat Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        heat = created["days"][0]["heats"][0]
        results = [
            {
                "contestantId": entry["contestantId"],
                "marbleNumber": entry["marbles"][0]["number"],
                "finish": index + 1,
            }
            for index, entry in enumerate(heat["entries"])
        ]
        rejected = self.client.put(
            f'/api/heats/{heat["id"]}/results', json={"results": results}
        )
        self.assertEqual(rejected.status_code, 409)
        self.assertIn("Start this heat", rejected.get_json()["error"])

        started = self.client.put(f'/api/heats/{heat["id"]}/start')
        self.assertEqual(started.status_code, 200)
        accepted = self.client.put(
            f'/api/heats/{heat["id"]}/results', json={"results": results}
        )
        self.assertEqual(accepted.status_code, 200)

    def test_staging_heats_lock_until_earlier_rounds_complete(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "Lock Order Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        heats = [heat for day in created["days"] for heat in day["heats"]]
        heat1, heat2 = heats[0], heats[1]
        self.assertFalse(heat1["locked"])
        self.assertFalse(heat1["started"])
        self.assertTrue(heat2["locked"])
        self.assertFalse(heat2["started"])

        blocked = self.client.put(f'/api/heats/{heat2["id"]}/start')
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("Complete the earlier rounds", blocked.get_json()["error"])

        state = score_heat_sequentially(self.client, heat1).get_json()
        heat1_after = state["days"][0]["heats"][0]
        heat2_after = state["days"][0]["heats"][1]
        self.assertTrue(heat1_after["complete"])
        self.assertFalse(heat1_after["locked"])
        self.assertFalse(heat2_after["locked"])
        self.assertFalse(heat2_after["started"])

        unlocked_start = self.client.put(f'/api/heats/{heat2["id"]}/start')
        self.assertEqual(unlocked_start.status_code, 200)

    def test_championship_heats_are_never_locked_or_gated_on_start(self) -> None:
        created = self.client.post(
            "/api/tournaments", json={"name": "No Start Needed Cup"}
        ).get_json()
        tournament_id = created["competition"]["id"]
        self.addCleanup(
            lambda: self.client.delete(f"/api/tournaments/{tournament_id}").close()
        )
        staging_heats = [heat for day in created["days"] for heat in day["heats"]]
        state = score_all_heats_sequentially(self.client, staging_heats)
        self.assertTrue(state["championship"]["wildcard"]["ready"])
        wildcard_heats = state["championship"]["wildcard"]["heats"]
        self.assertTrue(wildcard_heats)
        for heat in wildcard_heats:
            self.assertNotIn("locked", heat)
            self.assertNotIn("started", heat)

        heat = wildcard_heats[0]
        results = []
        position = 0
        for entry in heat["entries"]:
            for race_marble in entry["marbles"]:
                position += 1
                results.append(
                    {
                        "contestantId": entry["contestantId"],
                        "marbleNumber": race_marble["number"],
                        "finish": position,
                    }
                )
        response = self.client.put(
            f'/api/heats/{heat["id"]}/results', json={"results": results}
        )
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
