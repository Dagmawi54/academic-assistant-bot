"""FSM state groups for all multi-step admin flows."""

from aiogram.fsm.state import State, StatesGroup


class SetupGroupStates(StatesGroup):
    """Group initial setup wizard."""

    waiting_group_select = State()  # Select which group to configure
    waiting_department = State()  # Enter department name
    waiting_year = State()  # Enter year (1-4)
    waiting_section = State()  # Enter section (A/B/C/...)
    waiting_semester = State()  # Enter semester (1/2)
    confirm = State()  # Confirm setup


class AddCourseStates(StatesGroup):
    """Add a new course to a group."""

    waiting_group_select = State()  # Select group
    waiting_course_name = State()  # Enter course name
    waiting_topic_select = State()  # Select topic to link
    confirm = State()  # Confirm creation


class LinkTopicStates(StatesGroup):
    """Link or re-link a course to a different topic."""

    waiting_group_select = State()
    waiting_course_select = State()
    waiting_topic_select = State()


class SemesterStates(StatesGroup):
    """Semester lifecycle management."""

    waiting_action = State()  # Close / Activate
    confirm_close = State()  # Confirm semester close
    waiting_new_semester = State()  # Enter new semester number
    confirm_activate = State()  # Confirm activation


class ExamCoverageStates(StatesGroup):
    """Exam coverage entry flow."""

    waiting_group_select = State()
    waiting_exam_type = State()  # Mid / Final / Quiz / Custom
    waiting_course = State()  # Select course
    waiting_chapters = State()  # Enter chapters
    waiting_notes = State()  # Optional notes
    confirm_post = State()  # Confirm posting


class PermissionStates(StatesGroup):
    """User role management."""

    waiting_group_select = State()
    waiting_user_id = State()  # Forward a message or enter user ID
    waiting_role = State()  # Select role
    confirm = State()


class AnnouncementStates(StatesGroup):
    """Admin announcement broadcasting."""

    waiting_group_select = State()
    waiting_destination = State()  # General topic / specific course / all
    waiting_content = State()  # Enter announcement text
    confirm = State()
