"""Inline keyboard builders for admin menus."""

from typing import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.database.models import Course, Group, Topic


def main_menu(has_groups: bool = True) -> InlineKeyboardMarkup:
    """Top-level admin menu."""
    buttons = [
        [InlineKeyboardButton(text="➕ Register / Setup New Group", callback_data="menu:setup_group")],
    ]
    if has_groups:
        buttons.extend([
            [InlineKeyboardButton(text="📚 Add/Manage Courses", callback_data="menu:cat_courses")],
            [InlineKeyboardButton(text="📢 Broadcasts & Announcements", callback_data="menu:cat_communications")],
            [InlineKeyboardButton(text="📅 Events & Reminders", callback_data="menu:cat_events")],
            [InlineKeyboardButton(text="⚙️ Admin & Permissions", callback_data="menu:cat_administration")],
            [InlineKeyboardButton(text="📊 Analytics & Logs", callback_data="menu:cat_analytics")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def unregistered_menu() -> InlineKeyboardMarkup:
    """Menu shown to users with no admin access, letting them set up a group."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Setup a New Group", callback_data="menu:setup_group")]]
    )


def cat_infrastructure() -> InlineKeyboardMarkup:
    return cat_courses()


def cat_courses() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="Add Course", callback_data="menu:add_course")],
        [InlineKeyboardButton(text="Semester Management", callback_data="menu:semester")],
        [InlineKeyboardButton(text="Back", callback_data="menu:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def cat_communications() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="Exam Coverage", callback_data="menu:exam_coverage")],
        [
            InlineKeyboardButton(text="AI Announcement", callback_data="menu:announcements"),
            InlineKeyboardButton(text="Raw Broadcast", callback_data="menu:broadcast"),
        ],
        [InlineKeyboardButton(text="Targeted Course Push", callback_data="menu:targeted_push")],
        [InlineKeyboardButton(text="Back", callback_data="menu:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def communications_menu() -> InlineKeyboardMarkup:
    """Backward-compatible alias for the communications category menu."""
    return cat_communications()


def cat_administration() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="Permissions & Roles", callback_data="menu:permissions")],
        [InlineKeyboardButton(text="Toggle AI Safety Filter", callback_data="menu:safety")],
        [InlineKeyboardButton(text="Back", callback_data="menu:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def cat_analytics() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="Analytics Overview", callback_data="menu:analytics_overview")],
        [InlineKeyboardButton(text="Recent Logs", callback_data="menu:logs_recent")],
        [InlineKeyboardButton(text="View Audit Logs", callback_data="menu:audit")],
        [InlineKeyboardButton(text="View System Metrics", callback_data="menu:metrics")],
        [InlineKeyboardButton(text="Version Info", callback_data="menu:cmd_version")],
        [InlineKeyboardButton(text="Back", callback_data="menu:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def cat_events() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📅 Upcoming Events", callback_data="menu:events_upcoming")],
        [
            InlineKeyboardButton(text="📚 Exams", callback_data="menu:events_exams"),
            InlineKeyboardButton(text="📝 Assignments", callback_data="menu:events_assignments"),
            InlineKeyboardButton(text="❓ Quizzes", callback_data="menu:events_quizzes"),
        ],
        [
            InlineKeyboardButton(text="⏰ Reminders", callback_data="menu:events_reminders"),
            InlineKeyboardButton(text="📖 Coverage", callback_data="menu:events_coverage"),
        ],
        [
            InlineKeyboardButton(text="⚠️ Review Queue", callback_data="menu:events_review"),
            InlineKeyboardButton(text="Recently Detected", callback_data="menu:events_recent"),
        ],
        [InlineKeyboardButton(text="Back", callback_data="menu:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)



def group_select(groups: Sequence[Group], prefix: str = "group") -> InlineKeyboardMarkup:
    """Select a group from user's administered groups. prefix allows dynamic routing."""
    buttons = []
    for group in groups:
        label = f"{group.department or 'Unknown'} Y{group.year or '?'} S{group.section or '?'}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"{prefix}:{group.id}")])
    buttons.append([InlineKeyboardButton(text="Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def department_select() -> InlineKeyboardMarkup:
    """Pre-set options for common departments plus custom."""
    buttons = [
        [InlineKeyboardButton(text="Computer Science", callback_data="dept:Computer Science")],
        [InlineKeyboardButton(text="Business Management", callback_data="dept:Business Management")],
        [InlineKeyboardButton(text="Engineering", callback_data="dept:Engineering")],
        [InlineKeyboardButton(text="Add Custom Department", callback_data="dept:custom")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def year_select() -> InlineKeyboardMarkup:
    """Select academic year."""
    buttons = [
        [InlineKeyboardButton(text=str(year), callback_data=f"year:{year}") for year in range(1, 6)],
        [InlineKeyboardButton(text="Cancel", callback_data="cancel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def semester_select() -> InlineKeyboardMarkup:
    """Select semester."""
    buttons = [
        [
            InlineKeyboardButton(text="Semester 1", callback_data="semester:1"),
            InlineKeyboardButton(text="Semester 2", callback_data="semester:2"),
        ],
        [InlineKeyboardButton(text="Cancel", callback_data="cancel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def topic_select(topics: Sequence[Topic]) -> InlineKeyboardMarkup:
    """Select from available active topics."""
    buttons = [
        [InlineKeyboardButton(text=topic.topic_name, callback_data=f"topic:{topic.id}")]
        for topic in topics
    ]
    buttons.append([InlineKeyboardButton(text="Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def topic_select_with_skip(topics: Sequence[Topic]) -> InlineKeyboardMarkup:
    """Select from available active topics, with a skip option."""
    buttons = [
        [InlineKeyboardButton(text=topic.topic_name, callback_data=f"topic:{topic.id}")]
        for topic in topics
    ]
    buttons.append([InlineKeyboardButton(text="Skip (no link)", callback_data="skip_topic")])
    buttons.append([InlineKeyboardButton(text="Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def cancel_only() -> InlineKeyboardMarkup:
    """Done button for course setup."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Done Adding Courses", callback_data="done_adding_courses")]
        ]
    )


def cancel_button() -> InlineKeyboardMarkup:
    """Single cancel button for active FSM flows."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Cancel", callback_data="cancel")]]
    )


def course_select(courses: Sequence[Course]) -> InlineKeyboardMarkup:
    """Select from available courses."""
    buttons = [
        [InlineKeyboardButton(text=course.course_name, callback_data=f"course:{course.id}")]
        for course in courses
    ]
    buttons.append([InlineKeyboardButton(text="Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def announcement_target_select(
    courses: Sequence[Course],
    *,
    selected_course_ids: set[int] | None = None,
) -> InlineKeyboardMarkup:
    """Select one or more announcement targets."""
    selected_course_ids = selected_course_ids or set()
    buttons = []
    for course in courses:
        marker = "✓ " if course.id in selected_course_ids else ""
        buttons.append(
            [InlineKeyboardButton(text=f"{marker}{course.course_name}", callback_data=f"ann:toggle_course:{course.id}")]
        )
    buttons.extend(
        [
            [InlineKeyboardButton(text="General Topic Only", callback_data="ann:target_general")],
            [InlineKeyboardButton(text="Global: All Configured Topics", callback_data="ann:target_global")],
            [InlineKeyboardButton(text="Continue", callback_data="ann:targets_done")],
            [InlineKeyboardButton(text="Cancel", callback_data="cancel")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def exam_type_select() -> InlineKeyboardMarkup:
    """Select exam type."""
    buttons = [
        [
            InlineKeyboardButton(text="Mid Exam", callback_data="exam_type:mid"),
            InlineKeyboardButton(text="Final Exam", callback_data="exam_type:final"),
        ],
        [
            InlineKeyboardButton(text="Quiz", callback_data="exam_type:quiz"),
            InlineKeyboardButton(text="Custom", callback_data="exam_type:custom"),
        ],
        [InlineKeyboardButton(text="Cancel", callback_data="cancel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def role_select() -> InlineKeyboardMarkup:
    """Select user role."""
    roles = [
        ("Owner", "owner"),
        ("Dept Admin", "dept_admin"),
        ("Section Admin", "section_admin"),
        ("Representative", "representative"),
        ("Moderator", "moderator"),
        ("Student", "student"),
    ]
    buttons = [
        [InlineKeyboardButton(text=label, callback_data=f"role:{value}")]
        for label, value in roles
    ]
    buttons.append([InlineKeyboardButton(text="Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def confirm_action(action_data: str) -> InlineKeyboardMarkup:
    """Generic confirm/cancel buttons."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Confirm", callback_data=f"confirm:{action_data}"),
                InlineKeyboardButton(text="Cancel", callback_data="cancel"),
            ]
        ]
    )


def semester_actions() -> InlineKeyboardMarkup:
    """Semester control actions."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Close Current Semester", callback_data="sem:close")],
            [InlineKeyboardButton(text="Activate New Semester", callback_data="sem:activate")],
            [InlineKeyboardButton(text="Cancel", callback_data="cancel")],
        ]
    )


def back_button() -> InlineKeyboardMarkup:
    """Single back-to-menu button."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Back to Menu", callback_data="menu:main")]]
    )
