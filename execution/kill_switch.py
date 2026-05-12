"""Kill switch: instant OFF, cancel entries, optionally manage exits."""

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from core.constants import EventType
from data.models import Event

logger = logging.getLogger(__name__)


class KillSwitch:
    """Manages the kill switch state and behavior."""

    def __init__(self, db_session_factory):
        self.db_session_factory = db_session_factory
        self._active = False
        self._manage_exits_while_off = True

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def manage_exits(self) -> bool:
        return self._manage_exits_while_off

    @manage_exits.setter
    def manage_exits(self, value: bool):
        self._manage_exits_while_off = value

    def activate(self, order_manager, session: Session, shadow_mode: bool = False):
        """Activate kill switch: stop new trades, cancel pending entries."""
        if self._active:
            return

        self._active = True
        logger.warning("KILL SWITCH ACTIVATED")

        # Cancel all pending entry orders
        cancelled = order_manager.cancel_all_entry_orders(session, shadow_mode)

        event = Event(
            event_type=EventType.KILL_SWITCH_ACTIVATED.name,
            message=f"Kill switch activated. Cancelled {cancelled} pending entries. "
                    f"Manage exits while off: {self._manage_exits_while_off}",
        )
        session.add(event)
        session.commit()

    def deactivate(self):
        """Deactivate kill switch: allow new trades again."""
        if not self._active:
            return

        self._active = False
        logger.info("KILL SWITCH DEACTIVATED")

        session = self.db_session_factory()
        try:
            event = Event(
                event_type=EventType.KILL_SWITCH_DEACTIVATED.name,
                message="Kill switch deactivated. New trades allowed.",
            )
            session.add(event)
            session.commit()
        except Exception as e:
            logger.error(f"Error logging kill switch deactivation: {e}")
            session.rollback()
        finally:
            session.close()

    @property
    def allow_new_trades(self) -> bool:
        """Whether new trades are allowed."""
        return not self._active

    @property
    def allow_exit_management(self) -> bool:
        """Whether exit management (stops, BE, EOD) should continue."""
        if not self._active:
            return True
        return self._manage_exits_while_off
