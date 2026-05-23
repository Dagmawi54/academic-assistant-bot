"""Communications DM handler — FSM-driven broadcasting and targeted pushes."""

from aiogram import Router, types, F, Bot
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import menus
from app.admin.states import AnnouncementStates
from app.database import crud
from app.database.models import Group, Course, Topic
from app.logging import get_logger
import html
logger = get_logger("communications_handler")
router = Router(name="communications")


@router.callback_query(F.data.in_({"menu:announcements", "menu:targeted_push"}))
async def start_communication(callback: types.CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """Start the broadcast or targeted push flow."""
    target_type = "broadcast" if callback.data == "menu:announcements" else "targeted"
    
    await state.clear()
    await state.update_data(target_type=target_type)

    user_id = callback.from_user.id
    managed_groups = await crud.get_managed_groups(session, user_id)

    if not managed_groups:
        await callback.message.edit_text(
            "❌ You do not manage any registered groups.",
            reply_markup=menus.back_button()
        )
        await callback.answer()
        return

    # If only 1 group, skip group selection
    if len(managed_groups) == 1:
        group = managed_groups[0]
        await _process_group_selection(callback, state, session, group.id)
    else:
        # Prompt for group selection
        await state.set_state(AnnouncementStates.waiting_group_select)
        await callback.message.edit_text(
            "🏢 <b>Select the group to send message to:</b>",
            reply_markup=menus.group_select(managed_groups),
            parse_mode="HTML"
        )
        await callback.answer()


@router.callback_query(AnnouncementStates.waiting_group_select, F.data.startswith("group:"))
async def cb_select_group(callback: types.CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    await _process_group_selection(callback, state, session, group_id)


async def _process_group_selection(callback: types.CallbackQuery, state: FSMContext, session: AsyncSession, group_id: int) -> None:
    await state.update_data(group_id=group_id)
    data = await state.get_data()
    target_type = data["target_type"]

    group = await crud.get_by_id(session, Group, group_id)

    if target_type == "broadcast":
        # Broadcast -> send to General topic
        general = await crud.get_general_topic(session, group_id)
        if not general:
            await callback.message.edit_text(
                "❌ This group has no registered General topic. Someone needs to send a message in General or type <code>/scan_topics</code> there first.",
                reply_markup=menus.cancel_only(),
                parse_mode="HTML"
            )
            await callback.answer()
            return
            
        await state.update_data(topic_id=general.id)
        await state.set_state(AnnouncementStates.waiting_content)
        await callback.message.edit_text(
            f"📢 <b>Broadcasting to {html.escape(group.department)}</b> (General)\n\n"
            "Send the message you want to broadcast (text, photo, document, etc.):\n"
            "_(Type or forward a message here)_",
            reply_markup=menus.cancel_only(),
            parse_mode="HTML"
        )
        await callback.answer()

    else:
        # Targeted push -> select course
        courses = await crud.get_active_courses(session, group_id)
        if not courses:
            await callback.message.edit_text(
                "❌ No active courses found in this group.",
                reply_markup=menus.cancel_only(),
                parse_mode="HTML"
            )
            await callback.answer()
            return

        await state.set_state(AnnouncementStates.waiting_destination)
        await callback.message.edit_text(
            f"🎯 <b>Targeted Push in {html.escape(group.department)}</b>\n\n"
            "Select the course to send the message to:",
            reply_markup=menus.course_select(courses),
            parse_mode="HTML"
        )
        await callback.answer()


@router.callback_query(AnnouncementStates.waiting_destination, F.data.startswith("course:"))
async def cb_select_course(callback: types.CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    course_id = int(callback.data.split(":")[1])
    course = await crud.get_by_id(session, Course, course_id)
    
    if not course.forum_topic_id:
        await callback.message.edit_text(
            f"❌ The course <b>{html.escape(course.course_name)}</b> is not linked to any forum topic.",
            reply_markup=menus.cancel_only(),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    await state.update_data(topic_id=course.forum_topic_id)
    await state.set_state(AnnouncementStates.waiting_content)
    await callback.message.edit_text(
        f"🎯 <b>Targeting: {html.escape(course.course_name)}</b>\n\n"
        "Send the message you want to push (text, photo, document, etc.):\n"
        "_(Type or forward a message here)_",
        reply_markup=menus.cancel_only(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(AnnouncementStates.waiting_content, F.chat.type == "private")
async def receive_content(message: types.Message, state: FSMContext) -> None:
    """Store the message ID that the user wants to broadcast."""
    # We store the message object ID to copy it later
    await state.update_data(message_id=message.message_id)
    
    data = await state.get_data()
    target_type = data["target_type"]

    await state.set_state(AnnouncementStates.confirm)
    
    action = "Broadcast" if target_type == "broadcast" else "Push"
    
    await message.answer(
        f"✅ Message received! Review it above.\n\n"
        f"Are you sure you want to {action.lower()} this message?",
        reply_markup=menus.confirm_action("send_push"),
        parse_mode="HTML"
    )

@router.callback_query(AnnouncementStates.confirm, F.data == "confirm:send_push")
async def confirm_send_push(callback: types.CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    """Execute the message copy to the target topic."""
    data = await state.get_data()
    topic_id = data["topic_id"]
    msg_id = data["message_id"]
    user_id = callback.from_user.id
    
    topic = await crud.get_by_id(session, Topic, topic_id)
    
    if not topic:
        await callback.message.edit_text(
            "❌ Error: Topic not found in database.",
            reply_markup=menus.back_button(),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    # Use copy_message to support all types (text, photos, docs)
    try:
        if topic.message_thread_id and topic.message_thread_id > 0:
            await bot.copy_message(
                chat_id=topic.chat_id,
                from_chat_id=callback.message.chat.id,
                message_id=msg_id,
                message_thread_id=topic.message_thread_id
            )
        else:
            await bot.copy_message(
                chat_id=topic.chat_id,
                from_chat_id=callback.message.chat.id,
                message_id=msg_id
            )
            
        await crud.log_action(
            session,
            action="communicate_push",
            telegram_user_id=user_id,
            details=f"Topic: {topic.topic_name} msg_id: {msg_id}"
        )
        
        await state.clear()
        await callback.message.edit_text(
            "✅ <b>Message successfully sent!</b>",
            reply_markup=menus.back_button(),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error("push_failed", error=str(e), chat_id=topic.chat_id, thread_id=topic.message_thread_id)
        await callback.message.edit_text(
            f"❌ <b>Failed to send message:</b>\n<code>{html.escape(str(e)[:150])}</code>\n\nEnsure I have send message permissions.",
            reply_markup=menus.back_button(),
            parse_mode="HTML"
        )
    
    await callback.answer()
