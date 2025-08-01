import anki
from anki.cards import Card
from anki.cards_pb2 import FsrsMemoryState
from anki.scheduler.v3 import QueuedCards
from anki.scheduler.v3 import Scheduler as V3Scheduler
from aqt import gui_hooks, mw
from aqt.reviewer import Reviewer, V3CardInfo

from .config_manager import get_config
from .fsrs.types import State
from .utils import get_knowledge_gain, get_last_review_date

Learning = anki.scheduler_pb2.SchedulingState.Learning
Review = anki.scheduler_pb2.SchedulingState.Review

config = get_config()

_get_next_v3_card_original = Reviewer._get_next_v3_card

cache = {}

queue_to_index = {
    QueuedCards.NEW: 0,
    QueuedCards.LEARNING: 1,
    QueuedCards.REVIEW: 2,
}


def _key_exp_knowledge_gain(x):
    # There is no need to cache this function, as it is only called once per card
    card = x.card
    deck_id = card.original_deck_id or card.deck_id

    if not card.memory_state:
        return 0

    state = State(
        float(card.memory_state.difficulty), float(card.memory_state.stability)
    )

    elapsed_days = cache["today"] - get_last_review_date(card)

    deck_config = mw.col.decks.config_dict_for_deck_id(deck_id)

    knowledge_gain = (
        get_knowledge_gain(state, elapsed_days=elapsed_days, deck_config=deck_config)
        or 0.0
    )

    return -knowledge_gain


def _get_next_v3_card_patched(self) -> None:
    """
    A patched version of Reviewer._get_next_v3_card.
    https://github.com/ankitects/anki/blob/208729fa3e3ecb261c359c9a75e83291e80b499d/qt/aqt/reviewer.py#L264
    """
    assert isinstance(self.mw.col.sched, V3Scheduler)
    output = self.mw.col.sched.get_queued_cards()
    if not output.cards:
        return
    self._v3 = V3CardInfo.from_queue(output)

    idx, counts = self._v3.counts()

    # TODO: Relearning cards will refresh the cache
    # TODO: Avoid reviewing cards too soon
    if idx in (QueuedCards.LEARNING, QueuedCards.REVIEW):
        deck_id = self.mw.col.decks.current()['id']

        if (
            getattr(self, "_cards_cached", None) is None
            or getattr(self, "_deck_id_cached", None) != deck_id
            or sum(counts[1:]) != len(self._cards_cached)
            or counts[queue_to_index[self._cards_cached[-1].queue]] <= 0
            or mw.col.sched.today != cache.get("today", None)
        ):
            # Refresh the cache
            self._deck_id_cached = deck_id

            # Fetch all cards
            extend_limits = self.mw.col.card_count()

            self.mw.col.sched.extend_limits(0, extend_limits)
            output_all = self.mw.col.sched.get_queued_cards(fetch_limit=extend_limits)
            self.mw.col.sched.extend_limits(0, -extend_limits)

            # Filter cards based on the queue
            cards = [
                card
                for card in output_all.cards
                if card.queue in (QueuedCards.LEARNING, QueuedCards.REVIEW)
            ]

            # Sort the cards by expected knowledge gain
            cache["today"] = mw.col.sched.today
            sorted_cards = sorted(cards, key=_key_exp_knowledge_gain)

            # Filter cards based on the counts
            filtered_counts = [0, 0, 0]
            filtered_cards = []
            for card in sorted_cards:
                index = queue_to_index[card.queue]
                if filtered_counts[index] < counts[index]:
                    filtered_cards.append(card)
                    filtered_counts[index] += 1

            # Make a stack of the filtered cards
            self._cards_cached = list(reversed(filtered_cards))
        else:
            # Disable undo
            self.mw.col.sched.extend_limits(0, 0)

        top_card = self._cards_cached[-1]

        # Update the V3CardInfo with the top card
        del self._v3.queued_cards.cards[:]
        self._v3.queued_cards.cards.extend([top_card])
        self._v3.states = top_card.states
        self._v3.states.current.custom_data = top_card.card.custom_data
        self._v3.context = top_card.context

    self.card = Card(self.mw.col, backend_card=self._v3.top_card().card)
    self.card.start_timer()


def _on_card_answered(reviewer, card, ease):
    if (
        getattr(reviewer, "_cards_cached", None)
        and reviewer._cards_cached[-1].card.id == card.id
    ):
        reviewer._cards_cached.pop()
    else:
        reviewer._cards_cached = None


def _on_card_buried(id: int) -> None:
    reviewer = mw.reviewer
    if (
        getattr(reviewer, "_cards_cached", None)
        and reviewer._cards_cached[-1].card.id == id
    ):
        reviewer._cards_cached.pop()
    else:
        reviewer._cards_cached = None


def _on_card_suspended(id: int) -> None:
    reviewer = mw.reviewer
    if (
        getattr(reviewer, "_cards_cached", None)
        and reviewer._cards_cached[-1].card.id == id
    ):
        reviewer._cards_cached.pop()
    else:
        reviewer._cards_cached = None


def _on_card_will_show(text: str, card: Card, kind: str) -> str:
    if kind != "reviewAnswer":
        return text

    if not config.disable_same_day_reviews:
        return text

    def _update_normal(normal):
        kind = normal.WhichOneof("kind")

        memory_state = None

        if kind == "review":
            if normal.review.HasField("memory_state"):
                memory_state = normal.review.memory_state
        elif kind == "learning":
            if normal.learning.HasField("memory_state"):
                memory_state = normal.learning.memory_state
        elif kind == "relearning":
            if normal.relearning.review.HasField("memory_state"):
                memory_state = normal.relearning.review.memory_state
        else:
            raise ValueError(f"Unknown normal kind: {kind}")

        if memory_state is None:
            return

        state = State(float(memory_state.difficulty), float(memory_state.stability))

        # If the algorithm fails to converge, fall back to the default behavior
        if kind == "learning":
            normal.ClearField(kind)
            normal.review.CopyFrom(
                Review(
                    scheduled_days=1,
                    memory_state=FsrsMemoryState(
                        difficulty=state.difficulty,
                        stability=state.stability,
                    ),
                )
            )
        elif kind == "relearning":
            scheduled_days = normal.relearning.review.scheduled_days
            normal.ClearField(kind)
            normal.review.CopyFrom(
                Review(
                    scheduled_days=scheduled_days,
                    memory_state=FsrsMemoryState(
                        difficulty=state.difficulty,
                        stability=state.stability,
                    ),
                )
            )

    _update_normal(mw.reviewer._v3.states.again.normal)
    _update_normal(mw.reviewer._v3.states.hard.normal)
    _update_normal(mw.reviewer._v3.states.good.normal)
    _update_normal(mw.reviewer._v3.states.easy.normal)

    return text


def update_reordering():
    if config.reorder_cards:
        Reviewer._get_next_v3_card = _get_next_v3_card_patched
    else:
        Reviewer._get_next_v3_card = _get_next_v3_card_original


def init_reordering():
    update_reordering()
    gui_hooks.reviewer_did_answer_card.append(_on_card_answered)
    gui_hooks.reviewer_will_bury_card.append(_on_card_buried)
    gui_hooks.reviewer_will_suspend_card.append(_on_card_suspended)
    gui_hooks.card_will_show.append(_on_card_will_show)
