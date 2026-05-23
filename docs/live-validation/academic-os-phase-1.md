# Academic OS Phase 1 Live Telegram Validation

Use this checklist against a real registered Telegram supergroup with active forum topics mapped to courses.

## Environment

- Date/time:
- Bot deployment/runtime:
- Group chat id:
- Admin Telegram user id:
- Active semester:
- Database:

## Topic Setup

| Course | Topic name | message_thread_id | Topic status | Course active |
| --- | --- | ---: | --- | --- |
| Math |  |  | active | yes |
| Data Structures |  |  | active | yes |

## Required Message Checks

For each message, confirm:

- Visible detection acknowledgement appears in the same Telegram topic.
- `AcademicItem` row exists.
- Course is inferred from topic when not present in text.
- Reminder rows exist when a deadline exists.
- APScheduler jobs exist for pending reminders.
- Events dashboard shows the item.
- Scheduled Reminders dashboard shows reminder timestamps.
- Scheduler Jobs dashboard shows `reminder_{id}` jobs.
- Source Telegram link opens the original message.

| Message | Topic/Course | AcademicItem id | Detected type | Inferred course | Reminder ids | Scheduler job ids | Dashboard visible | Ack copied text |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| `exam on monday` | Math |  | exam | Math |  |  |  |  |
| `assignment due friday` | Data Structures |  | assignment | Data Structures |  |  |  |  |
| `covers chapter 1-4` | Math |  | exam_coverage | Math | n/a | n/a |  |  |
| `excluding recursion` | Data Structures |  | exam_coverage | Data Structures | n/a | n/a |  |  |

## Reminder Delivery Check

Create a near-future event in a course topic and temporarily configure a reminder offset that fires during validation.

| Message | Course/topic | AcademicItem id | Reminder id | Expected fire time | Delivered to thread_id | Delivered text |
| --- | --- | ---: | ---: | --- | ---: | --- |
|  |  |  |  |  |  |  |

## Duplicate Suppression Check

Send a duplicate academic message with the same course and similar deadline.

| Original item id | Duplicate message | DuplicateLog id | Dashboard visible | Suppression reason |
| ---: | --- | ---: | --- | --- |
|  |  |  |  |  |

## Result

- Pass/fail:
- Issues found:
- Follow-up fixes:
