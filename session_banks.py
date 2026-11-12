"""Stable session positions and independent pages for each input source."""

from dataclasses import dataclass
import threading


SOURCES = ("claude-code", "codex", "agent-shell")


@dataclass(frozen=True)
class SlotAddress:
    source: str
    index: int


class SessionBanks:
    def __init__(self, page_size):
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        self.num_slots = page_size
        self.session_to_slot = {}
        self._banks = {source: [] for source in SOURCES}
        self._pages = {source: 0 for source in SOURCES}
        self.selected_source = SOURCES[0]
        self.lock = threading.RLock()

    def allocate(self, session_id, source):
        with self.lock:
            if session_id in self.session_to_slot:
                return self.session_to_slot[session_id]
            if source not in self._banks:
                return None
            bank = self._banks[source]
            index = next((i for i, value in enumerate(bank) if value is None), len(bank))
            if index == len(bank):
                bank.append(session_id)
            else:
                bank[index] = session_id
            address = SlotAddress(source, index)
            self.session_to_slot[session_id] = address
            return address

    def slot_for(self, session_id):
        with self.lock:
            return self.session_to_slot.get(session_id)

    def free(self, session_id):
        with self.lock:
            address = self.session_to_slot.pop(session_id, None)
            if address is None:
                return None
            bank = self._banks[address.source]
            bank[address.index] = None
            while bank and bank[-1] is None:
                bank.pop()
            self._pages[address.source] = min(
                self._pages[address.source], self.page_count(address.source) - 1
            )
            return address

    def evict(self, physical_index):
        with self.lock:
            session = self.slot_to_session[physical_index]
            if session is not None:
                self.free(session)
            return session

    def page_count(self, source=None):
        with self.lock:
            count = len(self._banks[source or self.selected_source])
            return max(1, (count + self.num_slots - 1) // self.num_slots)

    @property
    def view(self):
        with self.lock:
            return self.selected_source, self._pages[self.selected_source]

    @property
    def slot_to_session(self):
        with self.lock:
            source, page = self.view
            start = page * self.num_slots
            entries = self._banks[source][start:start + self.num_slots]
            return entries + [None] * (self.num_slots - len(entries))

    def physical_index(self, address):
        with self.lock:
            source, page = self.view
            if address.source != source or address.index // self.num_slots != page:
                return None
            return address.index % self.num_slots

    def select_bank(self, index):
        with self.lock:
            if not 0 <= index < len(SOURCES):
                raise ValueError("unknown source bank")
            self.selected_source = SOURCES[index]

    def next_page(self):
        with self.lock:
            source = self.selected_source
            self._pages[source] = (self._pages[source] + 1) % self.page_count()
