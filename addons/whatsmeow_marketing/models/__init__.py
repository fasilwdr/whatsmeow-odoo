# The subscription model owns the table the contact/list many2many points at,
# so it must be set up first: whichever comes first creates the table, and a
# plain m2m table has no `id` column for the model to live in. Same note
# mass_mailing carries over mailing_subscription.
from . import whatsmeow_broadcast_subscription
from . import whatsmeow_broadcast_contact
from . import whatsmeow_broadcast_list
from . import whatsmeow_marketing_filter
from . import whatsmeow_marketing_campaign
from . import whatsmeow_marketing_trace
from . import whatsmeow_message
from . import whatsmeow_session
from . import res_partner
