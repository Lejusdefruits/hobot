// Default cover-letter template (Typst, @preview/modernpro-coverletter).
//
// tools/documents.py::generate_letter_pdf prepends a small prelude of #let
// bindings above whatever template file is in use (this one by default, or
// COVER_LETTER_TEMPLATE_PATH in .env if set to your own .typ file), then
// appends the letter body itself as plain paragraphs below it. A custom
// template only needs to consume the same bindings -- the body-appended-
// after part of the contract stays the same either way:
//   hobot_name, hobot_role, hobot_address   -- your profile (any can be `none`)
//   hobot_contacts                          -- array of (text: ..., link: ...)
//   hobot_recipient_name, hobot_recipient_address, hobot_subject
//                                            -- the company/location/offer
//                                               title (any can be `none`)
//   hobot_date                              -- already formatted in the
//                                               letter's own language
#import "@preview/modernpro-coverletter:1.0.1": *

#show: coverletter.with(
  profile: (
    name: hobot_name,
    role: hobot_role,
    address: hobot_address,
    contacts: hobot_contacts,
  ),
  recipient: (
    name: hobot_recipient_name,
    address: hobot_recipient_address,
    subject: hobot_subject,
    date: hobot_date,
  ),
  // No salutation word: the letter's own closing line (written to match
  // whichever language the offer itself is in) already reads naturally
  // right before the template's own bold name signature below it.
  closing: (salutation: none),
)
