from anki.cards import Card
from anki.scheduler.v3 import QueuedCards
from anki.scheduler.v3 import Scheduler as V3Scheduler
from aqt import gui_hooks, mw
from aqt.reviewer import Reviewer, V3CardInfo

from .config_manager import get_config
from .fsrs_utils.types import State
from .knowledge_ema.fsrs4 import exp_knowledge_gain as exp_knowledge_gain_v4
from .knowledge_ema.fsrs5 import exp_knowledge_gain as exp_knowledge_gain_v5
from .knowledge_ema.fsrs6 import exp_knowledge_gain as exp_knowledge_gain_v6
from .utils import (
    get_last_review_date,
    is_valid_fsrs4_params,
    is_valid_fsrs5_params,
    is_valid_fsrs6_params,
)

config = get_config()

_get_next_v3_card_original = Reviewer._get_next_v3_card

cache = {}

queue_to_index = {
    QueuedCards.NEW: 0,
    QueuedCards.LEARNING: 1,
    QueuedCards.REVIEW: 2,
}


def _key_exp_knowledge_gain(x):
    card = x.card
    deck_id = card.original_deck_id or card.deck_id

    if not card.memory_state:
        return 0

    state = State(
        float(card.memory_state.difficulty), float(card.memory_state.stability)
    )

    elapsed_days = cache["today"] - get_last_review_date(card)

    deck_config = mw.col.decks.config_dict_for_deck_id(deck_id)
    fsrs_params_v6 = deck_config.get("fsrsParams6")
    fsrs_params = deck_config.get("fsrsWeights")

    if is_valid_fsrs6_params(fsrs_params_v6):
        return -exp_knowledge_gain_v6(state, tuple(fsrs_params_v6), elapsed_days)
    elif is_valid_fsrs5_params(fsrs_params):
        return -exp_knowledge_gain_v5(state, tuple(fsrs_params), elapsed_days)
    elif is_valid_fsrs4_params(fsrs_params):
        return -exp_knowledge_gain_v4(state, tuple(fsrs_params), elapsed_days)
    else:
        return 0


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
