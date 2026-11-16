"""Rank live audience signals before handing a moment to the clip cutter."""

from collections import deque
from dataclasses import dataclass, field
from statistics import median


MATCH_SECONDS = 15
SETTLE_SECONDS = 180
RANK_INTERVAL = 60
VIEWER_LOOKBACK = 1800
CHAT_WINDOW_SECONDS = 5
CHAT_BASELINE_SECONDS = 180
MIN_BASELINE_WINDOWS = 12
QUALITY_SETTLE_SECONDS = 240
CHAT_LATENCY_SECONDS = 8
EXCEPTIONAL_REACTORS = 12
EXCEPTIONAL_LAUGHERS = 8
EXCEPTIONAL_SHARE = 0.25
EXCEPTIONAL_STRENGTH = 3
CONSENSUS_SPREAD_SECONDS = 10


class ChatShare:
    """Compare a five-second window with earlier windows from the same chat."""

    def __init__(self):
        self.history = deque()

    def observe(self, now, active, reactors):
        while self.history and self.history[0][0] < now - CHAT_BASELINE_SECONDS:
            self.history.popleft()
        share = reactors / active if active else 0.0
        prior = [value for end, value in self.history if end <= now - CHAT_WINDOW_SECONDS]
        if active >= 5 and (
            not self.history or now - self.history[-1][0] >= CHAT_WINDOW_SECONDS
        ):
            self.history.append((now, share))
        if len(prior) < MIN_BASELINE_WINDOWS:
            return None
        baseline = median(prior)
        return {
            "reaction_active": active,
            "reaction_reactors": reactors,
            "reaction_share": share,
            "reaction_baseline": baseline,
            "reaction_baseline_windows": len(prior),
            "reaction_strength": share / max(0.05, baseline),
        }


def strong_chat(event):
    return (event["reaction_reactors"] >= 6 and event["reaction_strength"] >= 2
            and event["reaction_share"] >= event["reaction_baseline"] + 0.10)


def chat_score(event):
    lift = min(event["reaction_strength"], 5)
    gain = max(0, event["reaction_share"] - event["reaction_baseline"])
    return lift + 4 * gain + min(event["reaction_laughers"], 10) / 2


def signal_distance(left, right, quality_first=False):
    left_anchor = left.get("anchor", left["timestamp"])
    right_anchor = right.get("anchor", right["timestamp"])
    distance = abs(left_anchor - right_anchor)
    if left["trigger"] == right["trigger"]:
        return distance
    viewer, chat = (left, right) if left["trigger"] == "viewer_clip" else (right, left)
    # A Twitch cut usually includes lead-in. Look for the reaction in its
    # latter portion or a few seconds after it to allow for chat latency.
    end = viewer["timestamp"]
    duration = viewer["viewer_clip"]["duration"]
    if quality_first:
        # Respect each clip's actual duration, but require the cue to be near
        # the clip end so long clips do not absorb unrelated reactions.
        if end - duration <= chat["anchor"] <= end + CHAT_LATENCY_SECONDS:
            return abs(end - chat["anchor"]) / 2
    elif end - min(duration, 25) <= chat["anchor"] <= end + 5:
        return distance / 2
    return float("inf")


@dataclass
class Moment:
    first_event: dict
    observed_at: float
    viewer_events: dict = field(default_factory=dict)
    chat: dict | None = None
    quality_first: bool = False
    selected: bool = False

    @property
    def anchor(self):
        return self.first_event.get("anchor", self.first_event["timestamp"])

    def distance(self, event):
        # Compare against the original signal so a chain of overlapping
        # clips cannot extend one moment over an entire broadcast.
        return signal_distance(event, self.first_event, self.quality_first)

    def add(self, event):
        if event["trigger"] == "viewer_clip":
            self.viewer_events.setdefault(event["viewer_creator"], event)
        elif self.chat is None or (strong_chat(event), chat_score(event)) > (
            strong_chat(self.chat), chat_score(self.chat),
        ):
            self.chat = event

    def evidence(self):
        creators = len(self.viewer_events)
        qualified_chat = self.chat is not None and strong_chat(self.chat)
        score = 2 * min(creators, 5)
        if qualified_chat:
            score += chat_score(self.chat)
        if creators and qualified_chat:
            score += 2
        return {
            "viewer_creators": creators,
            "chat_reactors": self.chat["reaction_reactors"] if self.chat else 0,
            "chat_active": self.chat["reaction_active"] if self.chat else 0,
            "chat_share": round(self.chat["reaction_share"], 4) if self.chat else None,
            "chat_baseline": round(self.chat["reaction_baseline"], 4) if self.chat else None,
            "chat_baseline_windows": self.chat["reaction_baseline_windows"] if self.chat else 0,
            "chat_observed_at": self.chat["timestamp"] if self.chat else None,
            "chat_anchor": self.chat["anchor"] if self.chat else None,
            "chat_qualified": qualified_chat,
            "chat_strength": round(self.chat["reaction_strength"], 2) if self.chat else 0,
            "chat_laughers": self.chat["reaction_laughers"] if self.chat else 0,
            "score": round(score, 2),
            "eligible": creators >= 2 or qualified_chat,
        }

    def quality_evidence(self):
        """Return a conservative export decision and its evidence trail."""
        evidence = self.evidence()
        chat = self.chat
        if chat is not None and "onset_anchor" in chat:
            evidence["chat_reaction_onset"] = chat["reaction_onset"]
            evidence["chat_onset_anchor"] = chat["onset_anchor"]
        corroborated = evidence["viewer_creators"] >= 2 and evidence["chat_qualified"]
        exceptional = bool(
            chat
            and evidence["chat_baseline_windows"] >= MIN_BASELINE_WINDOWS
            and evidence["chat_reactors"] >= EXCEPTIONAL_REACTORS
            and evidence["chat_laughers"] >= EXCEPTIONAL_LAUGHERS
            and evidence["chat_share"] >= EXCEPTIONAL_SHARE
            and evidence["chat_strength"] >= EXCEPTIONAL_STRENGTH
            and evidence["chat_share"] >= evidence["chat_baseline"] + 0.10
        )
        evidence["eligible"] = corroborated or exceptional
        evidence["decision"] = "selected" if evidence["eligible"] else "rejected"
        if corroborated:
            evidence["reason"] = "strong_chat_with_independent_viewer_clips"
        elif exceptional:
            evidence["reason"] = "exceptional_chat_reaction"
        elif evidence["viewer_creators"] >= 2:
            evidence["reason"] = "viewer_clip_consensus_without_strong_chat"
        elif evidence["chat_qualified"]:
            evidence["reason"] = "strong_chat_without_independent_viewer_clips"
        elif chat:
            evidence["reason"] = "chat_reaction_below_quality_threshold"
        elif evidence["viewer_creators"]:
            evidence["reason"] = "viewer_clips_without_chat_corroboration"
        else:
            evidence["reason"] = "no_qualifying_signal"

        # Expose a conservative interval agreement estimate for downstream trim
        # tuning. It is guidance only; the media layer owns final bounds.
        intervals = sorted(
            (event["timestamp"] - event["viewer_clip"]["duration"], event["timestamp"])
            for event in self.viewer_events.values()
        )
        if len(intervals) >= 2:
            starts = [item[0] for item in intervals]
            ends = [item[1] for item in intervals]
            if max(starts) - min(starts) <= CONSENSUS_SPREAD_SECONDS and max(ends) - min(ends) <= CONSENSUS_SPREAD_SECONDS:
                evidence["viewer_consensus_start"] = median(starts)
                evidence["viewer_consensus_end"] = median(ends)
                evidence["viewer_consensus_spread"] = max(max(starts) - min(starts), max(ends) - min(ends))
        return evidence

    def cut(self, evidence):
        # Prefer a viewer cut that lasts through the estimated reaction.
        # Otherwise keep the first distinct creator's cut.
        anchor = self.chat["anchor"] if self.chat else self.anchor
        event = min(self.viewer_events.values(),
                    key=lambda candidate: (candidate["timestamp"] < anchor,
                                           abs(candidate["timestamp"] - anchor)),
                    default=self.chat)
        cut = {key: value for key, value in event.items() if key != "viewer_creator"}
        if self.chat:
            cut["chat_sample"] = self.chat.get("chat_sample", [])
        return cut | {"ranking": evidence}


class MomentRanking:
    def __init__(self, quality_first=False):
        self.quality_first = quality_first
        self.pending: list[Moment] = []
        self.closed: list[Moment] = []
        self.next_rank = 0.0
        self.promoted: list[Moment] = []

    def add(self, event, now):
        if self.quality_first:
            closed_match = min(
                (moment for moment in self.closed if moment.distance(event) <= MATCH_SECONDS),
                key=lambda item: item.distance(event),
                default=None,
            )
            if closed_match is not None:
                if not closed_match.selected:
                    closed_match.add(event)
                    if closed_match.quality_evidence()["eligible"]:
                        closed_match.selected = True
                        self.promoted.append(closed_match)
                return
        moment = min(self.pending, key=lambda item: item.distance(event), default=None)
        if moment is None or moment.distance(event) > MATCH_SECONDS:
            if any(previous.distance(event) <= MATCH_SECONDS for previous in self.closed):
                return
            moment = Moment(event, now, quality_first=self.quality_first)
            self.pending.append(moment)
        moment.add(event)

    def rank(self, now):
        promoted = self.promoted if self.quality_first else []
        self.promoted = []
        promoted_cuts = [
            (moment.cut(moment.quality_evidence()), True)
            for moment in promoted
        ]
        self.closed = [moment for moment in self.closed if now - moment.anchor <= VIEWER_LOOKBACK]
        if now < self.next_rank:
            return promoted_cuts
        settle_seconds = QUALITY_SETTLE_SECONDS if self.quality_first else SETTLE_SECONDS
        ready = [moment for moment in self.pending if now - moment.observed_at >= settle_seconds]
        if not ready:
            return promoted_cuts
        self.next_rank = now + RANK_INTERVAL
        self.pending = [moment for moment in self.pending if now - moment.observed_at < settle_seconds]
        self.closed.extend(ready)
        ranked = [(moment, moment.quality_evidence() if self.quality_first else moment.evidence()) for moment in ready]
        ranked.sort(key=lambda item: (-item[1]["score"], item[0].anchor))
        if self.quality_first:
            # Every returned candidate is a distinct signal cluster. Keep all
            # passing clusters; there is deliberately no per-minute quota.
            for moment, evidence in ranked:
                moment.selected = evidence["eligible"]
            return promoted_cuts + [
                (moment.cut(evidence), evidence["eligible"])
                for moment, evidence in ranked
            ]
        winner = next((moment for moment, evidence in ranked if evidence["eligible"]), None)
        return [(moment.cut(evidence), moment is winner) for moment, evidence in ranked]
