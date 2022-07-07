import asyncio
import enum
import logging
import math
import random
import uuid
from datetime import datetime
from typing import Callable, Final, Iterable, List, Optional, Tuple

from ..protos import raft_pb2
from .client import RaftClient
from .protocol import RaftProtocol
from .server import RaftServer

__all__ = ('RaftFiniteStateMachine', 'RaftState')


class RaftState(enum.Enum):
    FOLLOWER = 0
    CANDIDATE = 1
    LEADER = 2


def randrangef(start: float, stop: float) -> float:
    return random.random() * (stop - start) + start


class RaftFiniteStateMachine(RaftProtocol):
    def __init__(
        self,
        peers: Iterable[str],
        server: RaftServer,
        client: RaftClient,
        *,
        on_state_changed: Optional[Callable] = None,
    ):
        self._id: Final[str] = str(uuid.uuid4())
        self._peers: Tuple[str, ...] = tuple(peers)
        self._server: Final[RaftServer] = server
        self._client: Final[RaftClient] = client
        self._on_state_changed: Optional[Callable[[RaftState], None]] = on_state_changed

        # Persistent state on all servers
        # (Updated on stable storage before responding to RPCs)
        self._current_term: int = 0
        self._voted_for: Optional[str] = None
        self._log: List[raft_pb2.Log] = []  # type: ignore

        # Volatile state on all servers
        self._commit_index: int = 0
        self._last_applied: int = 0

        # Volatile state on leaders
        # (Reinitialized after election)
        self._next_index: List[int] = []
        self._match_index: List[int] = []

        self._election_timeout: Final[float] = randrangef(0.15, 0.3)
        self._heartbeat_interval: Final[float] = 0.1
        self._leader_id: Optional[str] = None

        self.execute_transition(RaftState.FOLLOWER)
        self._server.bind(self)

    async def main(self):
        while True:
            match self._state:
                case RaftState.FOLLOWER:
                    self.reset_timeout()
                    await self._wait_for_election_timeout()
                    self.execute_transition(RaftState.CANDIDATE)
                case RaftState.CANDIDATE:
                    self._leader_id = None
                    while self._state is RaftState.CANDIDATE:
                        await self._request_vote()
                        if self._state is RaftState.LEADER:
                            break
                        await asyncio.sleep(self._election_timeout)
                case RaftState.LEADER:
                    logging.info(f'[{datetime.now().isoformat()}] LEADER: {self.id}')
                    self._leader_id = self.id
                    while self._state is RaftState.LEADER:
                        await self._publish_heartbeat()
                        await asyncio.sleep(self._heartbeat_interval)
            await asyncio.sleep(0)

    def execute_transition(self, next_state: RaftState):
        self._state = next_state
        getattr(self._on_state_changed, '__call__', lambda _: None)(next_state)

    """
    RaftProtocol Implementations
    - on_append_entries
    - on_request_vote
    """
    def on_append_entries(
        self,
        *,
        term: int,
        leader_id: str,
        prev_log_index: int,
        prev_log_term: int,
        entries: Iterable[str],
        leader_commit: int,
    ) -> bool:
        """Receiver implementation:
        1. Reply false if term < currentTerm
        2. Reply false if log doesn't contain any entry at prevLogIndex whose term matches prevLogTerm
        3. If an existing entry conflicts with a new one (same index but different terms),
           delete the existing entry and all that follow it
        4. Append any new entries not already in the log
        5. If leaderCommit > commitIndex, set commitIndex = min(leaderCommit, index of last new entry)
        """
        if term < self.current_term:
            return False
        self._synchronize_term(term)
        self._leader_id = leader_id
        logging.debug(f'[{datetime.now().isoformat()}] [on_append_entries] term={term} leader={leader_id[:2]}')
        self.reset_timeout()
        return True

    def on_request_vote(
        self,
        *,
        term: int,
        candidate_id: str,
        last_log_index: int,
        last_log_term: int,
    ) -> bool:
        """Receiver implementation:
        1. Reply false if term < currentTerm
        2. If votedFor is null or candidateId, and candidate's log is at least up-to-date as receiver's log, grant vote
        """
        current_term = self.current_term
        self._synchronize_term(term)
        self.reset_timeout()
        if term < current_term:
            return False
        if self.voted_for is None:
            self._voted_for = candidate_id
            return True
        elif self.voted_for == candidate_id:
            return True
        return False

    def reset_timeout(self):
        self._elapsed_time: float = 0.0

    async def _wait_for_election_timeout(self, interval: float = 1.0 / 30):
        while self._elapsed_time < self._election_timeout:
            await asyncio.sleep(interval)
            self._elapsed_time += interval

    async def _request_vote(self):
        term = self.current_term
        self._synchronize_term(term + 1)
        self.execute_transition(RaftState.CANDIDATE)
        results = await asyncio.gather(*[
            asyncio.create_task(
                self._client.request_vote(
                    address=peer, term=term, candidate_id=self.id,
                    last_log_index=0, last_log_term=0,
                ),
            )
            for peer in self._peers
        ])
        if sum(results) + 1 >= self.quorum:
            self.execute_transition(RaftState.LEADER)

    async def _publish_heartbeat(self):
        await asyncio.wait({
            asyncio.create_task(
                self._client.request_append_entries(
                    address=peer, term=self._current_term,
                    leader_id=self.id, entries=(),
                    # timeout=heartbeat_interval,
                ),
            )
            for peer in self._peers
        }, return_when=asyncio.ALL_COMPLETED)

    def _synchronize_term(self, term: int):
        """Rules for Servers
        All Servers:
        - If RPC request or response contains term T > currentTerm: set currentTerm = T, convert to follower
        """
        if term > self.current_term:
            self._current_term = term
            self.execute_transition(RaftState.FOLLOWER)
            self._voted_for = None

    @property
    def id(self) -> str:
        return self._id

    @property
    def is_leader(self) -> bool:
        return self._leader_id == self._id

    @property
    def current_term(self) -> int:
        return self._current_term

    @property
    def voted_for(self) -> Optional[str]:
        return self._voted_for

    @property
    def membership(self) -> int:
        return len(self._peers) + 1

    @property
    def quorum(self) -> int:
        return math.floor(self.membership / 2) + 1