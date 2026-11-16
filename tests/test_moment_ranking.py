import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from moment_ranking import (ChatShare, MomentRanking, QUALITY_SETTLE_SECONDS,
                            RANK_INTERVAL, SETTLE_SECONDS, strong_chat)


def viewer(timestamp, creator, duration=30):
    return {"trigger": "viewer_clip", "timestamp": timestamp, "viewer_creator": creator,
            "viewer_clip": {"duration": duration}}


def chat(timestamp, reactors, strength, laughers=0, active=40):
    share = reactors / active
    return {"trigger": "chat_reaction", "timestamp": timestamp, "anchor": timestamp - 5,
            "reaction_reactors": reactors, "reaction_strength": strength,
            "reaction_active": active, "reaction_share": share,
            "reaction_baseline": share / strength, "reaction_baseline_windows": 12,
            "reaction_laughers": laughers}


class TestChatShare(unittest.TestCase):
    def test_compares_with_prior_nonoverlapping_windows_and_expires_baseline(self):
        model = ChatShare()
        for now in range(5, 65, 5):
            self.assertIsNone(model.observe(now, 60, 3))
        self.assertIsNone(model.observe(63, 60, 18))
        stats = model.observe(65, 60, 18)
        self.assertEqual(stats["reaction_baseline_windows"], 12)
        self.assertEqual(stats["reaction_baseline"], 0.05)
        self.assertEqual(stats["reaction_share"], 0.30)
        self.assertTrue(strong_chat(stats))
        self.assertIsNone(model.observe(300, 60, 18))

    def test_same_count_means_something_different_in_busy_chat(self):
        quiet, busy = ChatShare(), ChatShare()
        for now in range(5, 65, 5):
            quiet.observe(now, 30, 2)
            busy.observe(now, 150, 12)
        self.assertTrue(strong_chat(quiet.observe(65, 30, 15)))
        self.assertFalse(strong_chat(busy.observe(65, 150, 15)))
        self.assertFalse(strong_chat(quiet.observe(66, 3, 3)))


class TestMomentRanking(unittest.TestCase):
    def test_one_creator_cannot_qualify_even_with_multiple_clips(self):
        ranking = MomentRanking()
        ranking.add(viewer(105, "same"), 110)
        ranking.add(viewer(112, "same"), 115)
        self.assertEqual(ranking.rank(110 + SETTLE_SECONDS - 1), [])
        [(event, selected)] = ranking.rank(110 + SETTLE_SECONDS)
        self.assertFalse(selected)
        self.assertEqual(event["ranking"]["viewer_creators"], 1)
        self.assertNotIn("viewer_creator", event)

    def test_distinct_viewers_can_win_without_chat(self):
        ranking = MomentRanking()
        ranking.add(viewer(105, "first"), 110)
        ranking.add(viewer(110, "second"), 130)
        [(event, selected)] = ranking.rank(110 + SETTLE_SECONDS)
        self.assertTrue(selected)
        self.assertEqual(event["ranking"]["viewer_creators"], 2)
        ranking.add(viewer(115, "late"), 210)
        self.assertEqual(ranking.rank(210 + SETTLE_SECONDS), [])

    def test_ranks_agreement_above_chat_alone_and_keeps_chat_only_eligible(self):
        ranking = MomentRanking()
        ranking.add(chat(102, 12, 2), 150)
        ranking.add(chat(140, 3, 1), 150)
        ranking.add(viewer(141, "first"), 150)
        ranking.add(chat(142, 10, 2), 151)
        [(winner, selected), (runner_up, outranked)] = ranking.rank(151 + SETTLE_SECONDS)
        self.assertTrue(selected)
        self.assertEqual(winner["timestamp"], 141)
        self.assertEqual(winner["ranking"]["chat_reactors"], 10)
        self.assertEqual(winner["ranking"]["viewer_creators"], 1)
        self.assertFalse(outranked)
        self.assertTrue(runner_up["ranking"]["eligible"])
        ranking.add(chat(350, 10, 2), 350)
        [(later, selected)] = ranking.rank(350 + SETTLE_SECONDS + RANK_INTERVAL)
        self.assertTrue(selected)
        self.assertEqual(later["trigger"], "chat_reaction")

    def test_matches_reaction_to_viewer_footage_in_either_arrival_order(self):
        events = [chat(105, 10, 2), viewer(122, "one")]
        for order in (events, list(reversed(events))):
            with self.subTest(order=order):
                ranking = MomentRanking()
                for event in order:
                    ranking.add(event, 130)
                [(event, selected)] = ranking.rank(130 + SETTLE_SECONDS)
                self.assertTrue(selected)
                self.assertEqual(event["ranking"]["chat_reactors"], 10)
                self.assertEqual(event["ranking"]["viewer_creators"], 1)

    def test_single_creator_with_weak_chat_stays_ineligible(self):
        for reactors, strength, active in [(3, 1, 40), (8, 2.67, 100), (12, 1.5, 40)]:
            with self.subTest(reactors=reactors, strength=strength, active=active):
                ranking = MomentRanking()
                ranking.add(viewer(121, "one"), 140)
                ranking.add(chat(120, reactors, strength, active=active), 140)
                [(event, selected)] = ranking.rank(140 + SETTLE_SECONDS)
                self.assertFalse(selected)
                self.assertEqual(event["ranking"]["chat_reactors"], reactors)
                self.assertEqual(event["ranking"]["score"], 2)

    def test_prefers_laughter_when_comparing_qualified_chat(self):
        ranking = MomentRanking()
        ranking.add(chat(101, 10, 3, 0), 180)
        ranking.add(chat(150, 10, 2, 5), 180)
        [(winner, selected), (_, outranked)] = ranking.rank(180 + SETTLE_SECONDS)
        self.assertTrue(selected)
        self.assertFalse(outranked)
        self.assertEqual(winner["ranking"]["chat_laughers"], 5)


class TestQualityFirstRanking(unittest.TestCase):
    def test_viewer_consensus_alone_is_not_exported_and_explains_why(self):
        ranking = MomentRanking(quality_first=True)
        ranking.add(viewer(105, "one"), 110)
        ranking.add(viewer(110, "two"), 115)
        self.assertEqual(ranking.rank(110 + QUALITY_SETTLE_SECONDS - 1), [])
        [(event, selected)] = ranking.rank(110 + QUALITY_SETTLE_SECONDS)
        self.assertFalse(selected)
        self.assertFalse(event["ranking"]["eligible"])
        self.assertEqual(event["ranking"]["reason"], "viewer_clip_consensus_without_strong_chat")

    def test_strong_chat_and_independent_viewers_selects_with_interval_guidance(self):
        ranking = MomentRanking(quality_first=True)
        ranking.add(viewer(105, "one", duration=30), 110)
        ranking.add(viewer(108, "two", duration=32), 115)
        ranking.add(chat(110, 10, 3, laughers=4), 120)
        [(event, selected)] = ranking.rank(110 + QUALITY_SETTLE_SECONDS)
        self.assertTrue(selected)
        self.assertEqual(event["ranking"]["reason"], "strong_chat_with_independent_viewer_clips")
        self.assertEqual(event["ranking"]["viewer_consensus_start"], 75.5)
        self.assertEqual(event["ranking"]["viewer_consensus_end"], 106.5)

    def test_exceptional_chat_can_stand_alone_but_ordinary_chat_cannot(self):
        ranking = MomentRanking(quality_first=True)
        ranking.add(chat(110, 12, 4, laughers=8), 120)
        ranking.add(chat(210, 10, 3, laughers=5), 220)
        [(exceptional, selected), (ordinary, ordinary_selected)] = ranking.rank(
            220 + QUALITY_SETTLE_SECONDS
        )
        self.assertTrue(selected)
        self.assertEqual(exceptional["ranking"]["reason"], "exceptional_chat_reaction")
        self.assertFalse(ordinary_selected)
        self.assertEqual(ordinary["ranking"]["reason"], "strong_chat_without_independent_viewer_clips")

    def test_late_reaction_inside_actual_viewer_range_correlates(self):
        ranking = MomentRanking(quality_first=True)
        ranking.add(viewer(130, "one", duration=90), 140)
        ranking.add(viewer(135, "two", duration=88), 141)
        ranking.add(chat(120, 10, 3, laughers=4), 142)
        [(event, selected)] = ranking.rank(140 + QUALITY_SETTLE_SECONDS)
        self.assertTrue(selected)
        self.assertEqual(event["ranking"]["viewer_creators"], 2)
        self.assertEqual(event["ranking"]["reason"], "strong_chat_with_independent_viewer_clips")

    def test_legacy_matching_keeps_original_fixed_lead_in_limit(self):
        legacy = MomentRanking()
        legacy.add(viewer(130, "one", duration=90), 140)
        legacy.add(chat(80, 10, 3, laughers=4), 142)
        self.assertEqual(len(legacy.pending), 2)

    def test_quality_matching_does_not_absorb_early_cue_in_long_clip(self):
        ranking = MomentRanking(quality_first=True)
        ranking.add(viewer(130, "one", duration=90), 140)
        ranking.add(chat(80, 10, 3, laughers=4), 142)
        self.assertEqual(len(ranking.pending), 2)

    def test_late_viewer_vote_promotes_recently_closed_rejected_moment(self):
        ranking = MomentRanking(quality_first=True)
        ranking.add(chat(110, 10, 3, laughers=4), 120)
        ranking.add(viewer(110, "one"), 120)
        [(rejected, selected)] = ranking.rank(120 + QUALITY_SETTLE_SECONDS)
        self.assertFalse(selected)
        self.assertEqual(rejected["ranking"]["reason"], "strong_chat_without_independent_viewer_clips")

        ranking.add(viewer(122, "two"), 120 + QUALITY_SETTLE_SECONDS + 1)
        [(promoted, selected)] = ranking.rank(120 + QUALITY_SETTLE_SECONDS + 1)
        self.assertTrue(selected)
        self.assertEqual(promoted["ranking"]["viewer_creators"], 2)
        self.assertEqual(promoted["ranking"]["reason"], "strong_chat_with_independent_viewer_clips")

    def test_late_vote_does_not_reopen_already_selected_moment(self):
        ranking = MomentRanking(quality_first=True)
        ranking.add(chat(110, 10, 3, laughers=4), 120)
        ranking.add(viewer(110, "one"), 120)
        ranking.add(viewer(122, "two"), 121)
        [(selected_event, selected)] = ranking.rank(120 + QUALITY_SETTLE_SECONDS)
        self.assertTrue(selected)
        ranking.add(viewer(125, "three"), 120 + QUALITY_SETTLE_SECONDS + 1)
        self.assertEqual(ranking.rank(120 + QUALITY_SETTLE_SECONDS + 1), [])

    def test_keeps_distinct_quality_moments_without_per_minute_quota(self):
        ranking = MomentRanking(quality_first=True)
        for timestamp in (100, 200, 300):
            ranking.add(chat(timestamp, 12, 4, laughers=8), timestamp + 1)
        results = ranking.rank(301 + QUALITY_SETTLE_SECONDS)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(selected for _, selected in results))
