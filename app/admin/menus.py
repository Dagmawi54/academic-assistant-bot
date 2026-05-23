"""Inline keyboard builders for admin menus."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from typing import Sequence

from app.database.models import Course, Group, Topic


def main_menu() -> InlineKeyboardMarkup:
    """Top-level admin menu."""
    buttons = [
        [InlineKeyboardButton(text="🏢 Infrastructure", callback_data="menu:cat_infrastructure")],
        [InlineKeyboardButton(text="📋 Events", callback_data="menu:cat_events")],
        [InlineKeyboardButton(text="📢 Communications", callback_data="menu:cat_communications")],
        [InlineKeyboardButton(text="⚙️ Administration", callback_data="menu:cat_administration")],
        [InlineKeyboardButton(text="📊 Analytics & Logs", callback_data="menu:cat_analytics")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def unregistered_menu() -> InlineKeyboardMarkup:
    """Menu shown to users with no admin access, letting them set up a group."""
    buttons = [
        [InlineKeyboardButton(text="📋 Setup a New Group", callback_data="menu:setup_group")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def cat_infrastructure() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📋 Setup Group", callback_data="menu:setup_group")],
        [InlineKeyboardButton(text="📚 Add Course", callback_data="menu:add_course")],
        [InlineKeyboardButton(text="📅 Semester Control", callback_data="menu:semester")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="menu:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def cat_communications() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📝 Exam Coverage", callback_data="menu:exam_coverage")],
        [InlineKeyboardButton(text="📢 Announcements", callback_data="menu:announcements")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="menu:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def cat_administration() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="👥 Permissions & Roles", callback_data="menu:permissions")],
        [InlineKeyboardButton(text="🛡️ Toggle AI Safety Filter", callback_data="menu:safety")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="menu:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def cat_analytics() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📝 View Audit Logs", callback_data="menu:audit")],
        [InlineKeyboardButton(text="📊 View System Metrics", callback_data="menu:metrics")],
        [InlineKeyboardButton(text="📦 Version Info", callback_data="menu:cmd_version")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="menu:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def cat_events() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📅 Upcoming Events", callback_data="menu:events_upcoming")],
        [InlineKeyboardButton(text="📝 Exam Coverages", callback_data="menu:events_coverage")],
        [InlineKeyboardButton(text="⏰ Scheduled Reminders", callback_data="menu:events_reminders")],
        [InlineKeyboardButton(text="⚠️ Low Confidence / Review", callback_data="menu:events_review")],
        [InlineKeyboardButton(text="🗑️ Suppressed Duplicates", callback_data="menu:events_duplicates")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="menu:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def group_select(groups: Sequence[Group], prefix: str = "group") -> InlineKeyboardMarkup:
    """Select a group from user's administered groups. prefix allows dynamic routing."""
    buttons = []
    for g in groups:
        label = f"{g.department or 'Unknown'} Y{g.year or '?'} S{g.section or '?'}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"{prefix}:{g.id}")])
    buttons.append([InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def department_select() -> InlineKeyboardMarkup:
    """Pre-set options for common departments + custom."""
    buttons = [
        [InlineKeyboardButton(text="💻 Computer Science", callback_data="dept:Computer Science")],
        [InlineKeyboardButton(text="📊 Business Management", callback_data="dept:Business Management")],
        [InlineKeyboardButton(text="⚙️ Engineering", callback_data="dept:Engineering")],
        [InlineKeyboardButton(text="➕ Add Custom Department", callback_data="dept:custom")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def year_select() -> InlineKeyboardMarkup:
    """Select academic year (1-5)."""
    buttons = [
        [InlineKeyboardButton(text=str(y), callback_data=f"year:{y}") for y in range(1, 6)],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def semester_select() -> InlineKeyboardMarkup:
    """Select semester (1 or 2)."""
    buttons = [
        [
            InlineKeyboardButton(text="Semester 1", callback_data="semester:1"),
            InlineKeyboardButton(text="Semester 2", callback_data="semester:2"),
        ],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def topic_select(topics: Sequence[Topic]) -> InlineKeyboardMarkup:
    """Select from available active topics."""
    buttons = []
    for t in topics:
        label = f"💬 {t.topic_name}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"topic:{t.id}")])
    buttons.append([InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def topic_select_with_skip(topics: Sequence[Topic]) -> InlineKeyboardMarkup:
    """Select from available active topics, with a 'Skip' option."""
    buttons = []
    for t in topics:
        label = f"💬 {t.topic_name}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"topic:{t.id}")])
    buttons.append([InlineKeyboardButton(text="⏩ Skip (no link)", callback_data="skip_topic")])
    buttons.append([InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def cancel_only() -> InlineKeyboardMarkup:
    """Just a cancel/done button."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✅ Done Adding Courses", callback_data="done_adding_courses")]]
    )


def course_select(courses: Sequence[Course]) -> InlineKeyboardMarkup:
    """Select from available courses."""
    buttons = []
    for c in courses:
        buttons.append(
            [InlineKeyboardButton(text=f"📖 {c.course_name}", callback_data=f"course:{c.id}")]
        )
    buttons.append([InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")])
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
        [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")],
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
        [InlineKeyboardButton(text=label, callback_data=f"role:{value}")] for label, value in roles
    ]
    buttons.append([InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def confirm_action(action_data: str) -> InlineKeyboardMarkup:
    """Generic confirm/cancel buttons."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Confirm", callback_data=f"confirm:{action_data}"),
                InlineKeyboardButton(text="❌ Cancel", callback_data="cancel"),
            ]
        ]
    )


def semester_actions() -> InlineKeyboardMarkup:
    """Semester control actions."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔒 Close Current Semester", callback_data="sem:close")],
            [InlineKeyboardButton(text="🔓 Activate New Semester", callback_data="sem:activate")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")],
        ]
    )


def back_button() -> InlineKeyboardMarkup:
    """Single back-to-menu button."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="menu:main")]]
    )
