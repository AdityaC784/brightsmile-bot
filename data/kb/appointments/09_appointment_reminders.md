---
doc_id: DENT-010
title: Automated Appointment Reminders and Patient Notification Protocol (POL-REM-01)
category: Appointments
status: active
version: "1.2"
effective_date: "2026-01-01"
last_updated: "2026-01-15"
source_type: operational_guide
authority: Clinic Operations Manager
tags:
  - reminders
  - sms
  - whatsapp
  - email
  - notifications
  - opt_out
  - POL-REM-01
---

# Automated Appointment Reminders and Patient Notification Protocol (POL-REM-01)

## 1. Purpose and Communication Standards

BrightSmile Dental Care operates an automated multi-channel appointment notification system (`POL-REM-01`) designed to help patients track upcoming dental visits, prepare required documents, and modify bookings well ahead of cancellation (`POL-CAN-01`) and rescheduling (`POL-RES-01`) deadlines.

All transactional appointment communications are transmitted strictly for active care coordination in compliance with India's Digital Personal Data Protection (DPDP) Act, 2023 (`DENT-027`) and Telecom Commercial Communications Customer Preference Regulations (TCCCPR). BrightSmile Dental Care never embeds third-party advertisements in appointment reminders.

---

## 2. Reminder Schedule and Delivery Channels

Every confirmed appointment triggers a standardized sequence of automated alerts across **SMS**, **WhatsApp Business (`+91-20-5550-0140`)**, and **Email (`support@brightsmile.example`)**:

| Notification Stage | Trigger Timing | Delivery Channels | Core Content Included |
| :--- | :--- | :--- | :--- |
| **Stage 1: Booking Confirmation** | Immediately upon booking confirmation (and deposit receipt if applicable) | SMS + WhatsApp + Email | Booking Reference ID, assigned clinician (`STF-xxx`), date/time (IST), Baner Road location pin, and digital new-patient pre-registration link |
| **Stage 2: Advance Reminder** | **48 hours** before scheduled appointment start time | SMS + WhatsApp + Email | Slot confirmation prompt (`CONFIRM`), direct link to reschedule free of charge (`≥ 12 hrs` window) or cancel (`≥ 24 hrs` window), and parking bay details (`TW-01`–`TW-06`, `B1-14`–`B1-16`) |
| **Stage 3: Same-Day Final Reminder** | **4 hours** before scheduled appointment start time | SMS + WhatsApp | Chair time reminder, 15-minute grace period note (`POL-LAT-01`), reminder to carry government photo ID (new patients) or prior X-rays, and reception phone `+91-20-5550-0140` |
| **Stage 4: Post-Visit Summary & Follow-Up** | 2 hours after clinical checkout | WhatsApp + Email | Itemized tax invoice PDF, digital prescription/aftercare sheet link (`DENT-031`–`DENT-034`), and recommended recall interval |

*Note on Same-Day Bookings: If an appointment is booked less than 48 hours in advance, Stage 2 is omitted and the patient receives Stage 1 immediately followed by Stage 3 (4 hours prior, provided the booking is made more than 4 hours before the slot).*

---

## 3. Interactive Quick-Action Controls

1. **One-Tap Confirmation (`CONFIRM`):** Replying `CONFIRM` on WhatsApp or clicking the confirmation button in the 48-hour reminder updates the clinic reception dashboard so operatory instruments are staged in advance.
2. **Self-Service Reschedule Link:** Clicking the reschedule link inside the 48-hour notification opens `https://brightsmile.example/book` directly to the patient's booking record, enabling fee-free rescheduling up to **12 hours** prior (`POL-RES-01`) and fee-free cancellation up to **24 hours** prior (`POL-CAN-01`).
3. **Pre-Visit Digital Intake Form:** First-time patients receive a secure pre-visit form link in Stage 1 and Stage 2 allowing them to submit Medical & Allergy Disclosure Form `FRM-MED-01` online before arriving at Aura Business Plaza (`DENT-025`).

---

## 4. Channel Preferences, Opt-Out Rules, and Patient Responsibility

* **Primary Mandatory Channel:** At least one active communication channel (SMS or Email) must remain enabled for active appointment confirmations, schedule-change alerts, and deposit refund receipts (`REF-01`).
* **Channel Customization:** Patients may opt out of WhatsApp messages or switch their primary reminder preference between SMS and Email at any time by informing reception (`+91-20-5550-0140`) or emailing `privacy@brightsmile.example`.
* **Responsibility Regardless of Delivery:** While BrightSmile Dental Care logs transmission timestamps for all automated alerts, temporary mobile network congestion, DND (Do-Not-Disturb) carrier filtering, or full email inboxes do not waive the patient's responsibility to attend on time or meet the 24-hour cancellation (`POL-CAN-01`) and 12-hour rescheduling (`POL-RES-01`) deadlines.

---

## 5. Related Documents

* `DENT-006` (`05_appointment_booking.pdf`) — Booking confirmation and slot-hold deposit workflow (`POL-BKG-01`).
* `DENT-007` (`06_cancellation_policy.docx`) — 24-hour cancellation deadline (`POL-CAN-01`).
* `DENT-008` (`07_rescheduling_policy.docx`) — 12-hour rescheduling deadline (`POL-RES-01`).
* `DENT-009` (`08_late_arrival_policy.docx`) — 15-minute arrival grace period (`POL-LAT-01`).
* `DENT-027` (`26_privacy_and_data_handling.docx`) — Patient contact data governance under DPDP Act, 2023 (`POL-PRV-01`).