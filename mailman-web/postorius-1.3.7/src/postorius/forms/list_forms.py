# -*- coding: utf-8 -*-
# Copyright (C) 2017-2022 by the Free Software Foundation, Inc.
#
# This file is part of Postorius.
#
# Postorius is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# Postorius is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along with
# Postorius.  If not, see <http://www.gnu.org/licenses/>.
#

import re
import os
from django.utils.safestring import mark_safe
from django import forms
from django.apps import apps
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils.translation import gettext_lazy as _

from django_mailman3.lib.mailman import get_mailman_client

from postorius.forms.fields import (
    ACTION_CHOICES, ListOfStringsField, delivery_mode_field,
    delivery_status_field, moderation_action_field)
from postorius.forms.validators import validate_uuid_or_email
from postorius.models import EmailTemplate, _email_template_help_text
from postorius.utils import LANGUAGES


DIGEST_FREQUENCY_CHOICES = (
    ("daily", _("Daily")),
    ("weekly", _("Weekly")),
    ("quarterly", _("Quarterly")),
    ("monthly", _("Monthly")),
    ("yearly", _("Yearly"))
)

ROSTER_VISIBILITY_CHOICES = (
    ("moderators", _("Only mailinglist moderators")),
    ("members", _("Only mailinglist members")),
    ("public", _("Anyone")),
    )


EMPTY_STRING = ''


class ListNew(forms.Form):

    """
    Form fields to add a new list. Languages are hard coded which should
    be replaced by a REST lookup of available languages.
    """
    listname = forms.CharField(
        label=_('List Name'),
        required=True,
        error_messages={'required': _('Please enter a name for your list.'),
                        'invalid': _('Please enter a valid list name.')})
    mail_host = forms.ChoiceField()
    list_owner = forms.EmailField(
        label=_('Initial list owner address'),
        error_messages={
            'required': _("Please enter the list owner's email address.")},
        required=True)
    advertised = forms.ChoiceField(
        widget=forms.RadioSelect(),
        label=_('Advertise this list?'),
        error_messages={
            'required': _("Please choose a list type.")},
        required=True,
        choices=(
            (True, _("Advertise this list in list index")),
            (False, _("Hide this list in list index"))))
    list_style = forms.ChoiceField()
    description = forms.CharField(
        label=_('Short Description'),
        required=False)

    def __init__(self, domain_choices, style_choices, *args, **kwargs):
        super(ListNew, self).__init__(*args, **kwargs)
        self.fields["mail_host"] = forms.ChoiceField(
            widget=forms.Select(),
            label=_('Mail Host'),
            required=True,
            choices=domain_choices,
            error_messages={'required': _("Choose an existing Domain."),
                            'invalid': _("Choose a valid Mail Host")})
        self.fields["list_style"] = forms.ChoiceField(
            widget=forms.Select(),
            label=_('List Style'),
            required=True,
            choices=style_choices,
            error_messages={'required': _("Choose a List Style."),
                            'invalid': _("Choose a valid List Style.")})
        if len(domain_choices) < 2:
            self.fields["mail_host"].help_text = _(
                "Site admin has not created any domains")
            # if len(choices) < 2:
            #    help_text=_("No domains available: " +
            #                "The site admin must create new domains " +
            #                "before you will be able to create a list")

    def clean_listname(self):
        try:
            validate_email(self.cleaned_data['listname'] + '@example.net')
        except ValidationError:
            # TODO (maxking): Error should atleast point to what is a valid
            # listname. It may not always be obvious which characters aren't
            # allowed in a listname.
            raise forms.ValidationError(_("Please enter a valid listname"))
        return self.cleaned_data['listname']

    class Meta:

        """
        Class to handle the automatic insertion of fieldsets and divs.

        To use it: add a list for each wished fieldset. The first item in
        the list should be the wished name of the fieldset, the following
        the fields that should be included in the fieldset.
        """
        layout = [["List Details",
                   "listname",
                   "mail_host",
                   "list_style",
                   "list_owner",
                   "description",
                   "advertised"], ]


class ListSubscribe(forms.Form):
    """Form fields to join an existing list.
    """

    DELIVERY_STATUS_CHOICES = (("enabled", _('Enabled')),
                               ("by_user", _('Disabled')))

    subscriber = forms.ChoiceField(
        label=_('Your email address'),
        widget=forms.Select(),
        validators=[validate_uuid_or_email, ],
        help_text=_(
            'Subscribing via "Primary Address" will change subscription'
            ' address when you change your primary address.'),
        error_messages={
            'required': _('Please enter an email address.'),
            'invalid': _('Please enter a valid email address.')})

    display_name = forms.CharField(
        label=_('Your name (optional)'), required=False)

    delivery_mode = delivery_mode_field(default='regular')
    delivery_status = delivery_status_field(choices=DELIVERY_STATUS_CHOICES,
                                            widget=forms.RadioSelect)

    def __init__(self, user_emails, user_id, primary_email, *args, **kwargs):
        super(ListSubscribe, self).__init__(*args, **kwargs)
        choices = list((address, address)
                       for address in user_emails)
        if primary_email and user_id:
            choices.insert(
                0,
                (user_id, _('Primary Address ({})').format(primary_email)))
        self.fields['subscriber'].choices = choices


class ListAnonymousSubscribe(forms.Form):
    """Form fields to join an existing list as an anonymous user.
    """

    email = forms.CharField(
        label=_('Your email address'),
        validators=[validate_email],
        error_messages={
            'required': _('Please enter an email address.'),
            'invalid': _('Please enter a valid email address.')})

    display_name = forms.CharField(
        label=_('Your name (optional)'), required=False)

    privacy_check = forms.BooleanField(initial=False, required=True, label=_(mark_safe(
        'I have read and agree to the community <a href="%s">Privacy Statement</a> and '
        '<a href="%s">Community Conduct</a>.' % (os.getenv("PRIVACY_LINK", "https://www.openeuler.org/en/other/privacy/"), os.getenv("CONDUCT_LINK", "https://www.openeuler.org/en/community/conduct/"))
    )), widget=forms.CheckboxInput())

    data_storage_check = forms.BooleanField(initial=False, required=True, label=_(mark_safe(
        'I agree to the storage of personal data (email address) in %s for the provision of email subscription services.' % (os.getenv("DATA_STORAGE_LOCATION", "Hong Kong, China"))
    )), widget=forms.CheckboxInput())


class ListAnonymousUnsubscribe(forms.Form):
    """Form fields to join an existing list as an anonymous user.
    """

    email = forms.CharField(
        label=_('Your email address'),
        validators=[validate_email],
        error_messages={
            'required': _('Please enter an email address.'),
            'invalid': _('Please enter a valid email address.')})


class ListSettingsForm(forms.Form):
    """
    Base class for list settings forms.
    """
    mlist_properties = []

    def __init__(self, *args, **kwargs):
        self._mlist = kwargs.pop('mlist')
        super(ListSettingsForm, self).__init__(*args, **kwargs)


SUBSCRIPTION_POLICY_CHOICES = (
    ('open', _('Open')),
    ('confirm', _('Confirm')),
    ('moderate', _('Moderate')),
    ('confirm_then_moderate', _('Confirm, then moderate')),
)


class MemberPolicyForm(ListSettingsForm):
    """
    Policies related to members.
    """
    subscription_policy = forms.ChoiceField(
        label=_('Subscription Policy'),
        choices=SUBSCRIPTION_POLICY_CHOICES,
        help_text=_('Open: Subscriptions are added automatically\n'
                    'Confirm: Subscribers need to confirm the subscription '
                    'using an email sent to them\n'
                    'Moderate: Moderators will have to authorize '
                    'each subscription manually.\n'
                    'Confirm then Moderate: First subscribers have to confirm,'
                    ' then a moderator needs to authorize.'))

    unsubscription_policy = forms.ChoiceField(
        label=_('Un-Subscription Policy'),
        choices=SUBSCRIPTION_POLICY_CHOICES,
        help_text=_('Open: Un-Subscriptions happen automatically\n'
                    'Confirm: Subscribers need to confirm the un-subscription '
                    'using an email sent to them\n'
                    'Moderate: Moderators will have to authorize '
                    'each un-subscription manually.\n'
                    'Confirm then Moderate: First subscribers have to confirm,'
                    ' then a moderator needs to authorize.'))


class BounceProcessingForm(ListSettingsForm):
    """List's bounce processing settings."""

    forward_unrecognized_choices = (
        ('discard', _('Discard')),
        ('administrators', _('List Admins')),
        ('site_owner', _('Site Admin')),
    )

    process_bounces = forms.BooleanField(
        widget=forms.RadioSelect(choices=((True, _('Yes')), (False, _('No')))),
        required=False,
        label=_('Process Bounces'),
        help_text=_(
            'Specifies whether or not this list should do automatic'
            ' bounce processing.'))

    bounce_score_threshold = forms.IntegerField(
        min_value=0,
        label=_('Bounce score threshold'),
        required=False,
        help_text=_(
            'This is the bounce score above which a member\'s subscription '
            ' will be automatically disabled. When the subscription is '
            ' re-enabled, their bounce score will be reset to zero.'))

    bounce_info_stale_after = forms.CharField(
        label=_('Bounce info stale after'),
        required=False,
        help_text=_(
            'The number of days after which a member\'s bounce information'
            ' is considered stale. If no new bounces have been received in'
            ' the interim, the bounce score is reset to zero.'
            ' This value must be an integer. '))

    bounce_notify_owner_on_bounce_increment = forms.BooleanField(
        widget=forms.RadioSelect(choices=((True, _('Yes')), (False, _('No')))),
        required=False,
        label=_('Notify owner on bounce increment'),
        help_text=_(
            'This option controls whether or not the list owner is notified'
            ' when a member\'s bounce score is incremented, but to a value'
            ' less than their bounce threshold. '))

    bounce_notify_owner_on_disable = forms.BooleanField(
        widget=forms.RadioSelect(choices=((True, _('Yes')), (False, _('No')))),
        required=False,
        label=_('Notify owner on disable'),
        help_text=_(
            'This option controls whether or not the list owner is notified'
            ' when a member\'s subscription is automatically disabled due'
            ' to their bounce threshold being reached. '))

    bounce_notify_owner_on_removal = forms.BooleanField(
        widget=forms.RadioSelect(choices=((True, _('Yes')), (False, _('No')))),
        required=False,
        label=_('Notify owner on removal'),
        help_text=_(
            'This option controls whether or not the list owner is '
            'notified when a member is removed from the list after '
            'their disabled notifications have been exhausted. '))

    forward_unrecognized_bounces_to = forms.ChoiceField(
        choices=forward_unrecognized_choices,
        widget=forms.RadioSelect,
        label=_('Forward unrecognized bounces'),
        help_text=_('Discard: Unrecognized bounces will be discarded\n'
                    'List Admins: Send to the list owners and moderators\n'
                    'Site Admin: Send to the site\'s configured site_owner'))

    bounce_you_are_disabled_warnings_interval = forms.CharField(
        label=_('Bounce disabled warnings interval'),
        required=False,
        help_text=_(
            'The number of days between each disabled notification.'))

    bounce_you_are_disabled_warnings = forms.IntegerField(
        min_value=0,
        label=_('Bounce disable warnings'),
        required=False,
        help_text=_(
            'The number of notices a disabled member will receive before'
            ' their address is removed from the mailing list\'s roster. '
            'Set this to 0 to immediately remove an address from the list'
            ' once their bounce score exceeds the threshold. '
            'This value must be an integer. '))


class ArchiveSettingsForm(ListSettingsForm):
    """
    Set the general archive policy.
    """
    mlist_properties = ['archivers']

    archive_policy_choices = (
        ("public", _("Public archives")),
        ("private", _("Private archives")),
        ("never", _("Do not archive this list")),
    )

    archive_rendering_mode_choices = (
        ("text", _("Plain text")),
        ("markdown", _("Markdown text")),
    )

    archive_policy = forms.ChoiceField(
        choices=archive_policy_choices,
        widget=forms.RadioSelect,
        label=_('Archive policy'),
        help_text=_('Policy for archiving messages for this list'),
    )

    archivers = forms.MultipleChoiceField(
        widget=forms.CheckboxSelectMultiple,
        label=_('Active archivers'),
        required=False)  # May be empty if no archivers are desired.

    archive_rendering_mode = forms.ChoiceField(
        choices=archive_rendering_mode_choices,
        widget=forms.RadioSelect,
        required=False,
        label=_('Archive Rendering mode'),
        help_text=_('This option enables rendering of emails in archiver as '
                    'rich text with formatting based on markup in the email.'
                    '\nCurrently, this option is only supported by Hyperkitty.'
                    )
    )

    def __init__(self, *args, **kwargs):
        super(ArchiveSettingsForm, self).__init__(*args, **kwargs)
        archiver_opts = sorted(self._mlist.archivers.keys())
        self.fields['archivers'].choices = sorted(
            [(key, key) for key in archiver_opts])
        if self.initial:
            self.initial['archivers'] = [
                key for key in archiver_opts if self._mlist.archivers[key] is True]   # noqa
        # If Hyperkitty isn't installed, do not show the archive_rendering_mode
        # field since it doesn't apply to other archivers.
        if not apps.is_installed('hyperkitty'):
            del self.fields['archive_rendering_mode']

    def clean_archivers(self):
        result = {}
        for archiver, etc in self.fields['archivers'].choices:
            result[archiver] = archiver in self.cleaned_data['archivers']
        self.cleaned_data['archivers'] = result
        return result


class MessageAcceptanceForm(ListSettingsForm):
    """
    List messages acceptance settings.
    """
    acceptable_aliases = ListOfStringsField(
        label=_("Acceptable aliases"),
        required=False,
        help_text=_(
            'This is a list, one per line, of addresses and regexps matching '
            'addresses that are acceptable in To: or Cc: in lieu of the list '
            'posting address when `require_explicit_destination\' is enabled. '
            ' Entries are either email addresses or regexps matching email '
            'addresses.  Regexps are entries beginning with `^\' and are '
            'matched against every recipient address in the message. The '
            'matching is performed with Python\'s re.match() function, meaning'
            ' they are anchored to the start of the string.'))
    require_explicit_destination = forms.BooleanField(
        widget=forms.RadioSelect(choices=((True, _('Yes')), (False, _('No')))),
        required=False,
        label=_('Require Explicit Destination'),
        help_text=_(
            'This checks to ensure that the list posting address or an '
            'acceptable alias explicitly appears in a To: or Cc: header in '
            'the post.'))
    administrivia = forms.BooleanField(
        widget=forms.RadioSelect(choices=((True, _('Yes')), (False, _('No')))),
        required=False,
        label=_('Administrivia'),
        help_text=_(
            'Administrivia tests will check postings to see whether it\'s '
            'really meant as an administrative request (like subscribe, '
            'unsubscribe, etc), and will add it to the administrative '
            'requests queue, notifying the administrator of the new request, '
            'in the process.'))
    default_member_action = forms.ChoiceField(
        widget=forms.RadioSelect(),
        label=_('Default action to take when a member posts to the list'),
        error_messages={
            'required': _("Please choose a default member action.")},
        required=True,
        choices=ACTION_CHOICES,
        help_text=_(
            'Default action to take when a member posts to the list.\n'
            'Hold: This holds the message for approval by the list '
            'moderators.\n'
            'Reject: this automatically rejects the message by sending a '
            'bounce notice to the post\'s author. The text of the bounce '
            'notice can be configured by you.\n'
            'Discard: this simply discards the message, with no notice '
            'sent to the post\'s author.\n'
            'Accept: accepts any postings without any further checks.\n'
            'Default Processing: run additional checks and accept '
            'the message.'))
    default_nonmember_action = forms.ChoiceField(
        widget=forms.RadioSelect(),
        label=_('Default action to take when a non-member posts to the list'),
        error_messages={
            'required': _("Please choose a default non-member action.")},
        required=True,
        choices=ACTION_CHOICES,
        help_text=_(
            'When a post from a non-member is received, the message\'s sender '
            'is matched against the list of explicitly accepted, held, '
            'rejected (bounced), and discarded addresses. '
            'If no match is found, then this action is taken.'))
    emergency = forms.BooleanField(
        widget=forms.RadioSelect(choices=((True, _('Yes')), (False, _('No')))),
        required=False,
        label=_('Emergency Moderation'),
        help_text=_(
            'When this option is enabled, all list traffic is emergency'
            ' moderated, i.e. held for moderation. Turn this option on when'
            ' your list is experiencing a flamewar and you want a cooling off'
            ' period. '),
    )
    max_message_size = forms.IntegerField(
        min_value=0,
        label=_('Maximum message size'),
        required=False,
        help_text=_(
            'The maximum allowed message size in KB. '
            'This can be used to prevent emails with large attachments. '
            'A size of 0 disables the check.'))
    max_num_recipients = forms.IntegerField(
        min_value=0,
        label=_('Maximum number of recipients'),
        required=False,
        help_text=_(
            'If a post has this many or more explicit recipients (To: and '
            'Cc:), the post will be held for moderation. '
            'This can be used to prevent mass mailings from being accepted. '
            'A value of 0 disables the check.'))

    # TODO: Expose after this functionality actually works in Core.
    # max_days_to_hold = forms.IntegerField(
    #     min_value=0,
    #     label=_('Discard held posts after'),
    #     required=False,
    #     help_text=_(
    #         'No. of days after which held messages will be automatically'
    #         ' discarded.'))

    accept_these_nonmembers = ListOfStringsField(
        label=_("Accept these non-members"),
        required=False,
        help_text=_(
            'This is a list, one per line, of regexps matching '
            'addresses that are allowed to post to this mailing list without'
            ' subscribing to the list.'
            ' Entries are regexps beginning with `^\' and are matched against'
            ' the sender addresses in the message.'
            ' While non-regexp addresses can be entered here, it is preferred'
            ' to add the address as a nonmember and set the nonmember\'s '
            'Moderation to Default Processing.'))

    hold_these_nonmembers = ListOfStringsField(
        label=_("Hold these non-members"),
        required=False,
        help_text=_(
            'This is a list, one per line, of regexps matching '
            'nonmember addresses, posts from which are held automatically.'
            ' Entries are regexps beginning with `^\' and are matched against'
            ' the sender addresses in the message.'
            ' While non-regexp addresses can be entered here, it is preferred'
            ' to add the address as a nonmember and set the nonmember\'s '
            'Moderation to Hold.'))

    reject_these_nonmembers = ListOfStringsField(
        label=_("Reject these non-members"),
        required=False,
        help_text=_(
            'This is a list, one per line, of regexps matching '
            'nonmember addresses, posts from which are rejected with notice to'
            ' the sender.'
            ' Entries are regexps beginning with `^\' and are matched against'
            ' the sender addresses in the message.'
            ' While non-regexp addresses can be entered here, it is preferred'
            ' to add the address as a nonmember and set the nonmember\'s '
            'Moderation to Reject.'))

    discard_these_nonmembers = ListOfStringsField(
        label=_("Discard these non-members"),
        required=False,
        help_text=_(
            'This is a list, one per line, of regexps matching '
            'nonmember addresses, posts from which are discarded automatically'
            '. Entries are regexps beginning with `^\' and are matched against'
            ' the sender addresses in the message.'
            ' While non-regexp addresses can be entered here, it is preferred'
            ' to add the address as a nonmember and set the nonmember\'s '
            'Moderation to Discard.'))

    def clean_acceptable_aliases(self):
        # python's urlencode will drop this attribute completely if an empty
        # list is passed with doseq=True. To make it work for us, we instead
        # use an empty string to signify an empty value. In turn, Core will
        # also consider an empty value to be empty list for list-of-strings
        # field.
        if not self.cleaned_data['acceptable_aliases']:
            return EMPTY_STRING
        for alias in self.cleaned_data['acceptable_aliases']:
            if alias.startswith('^'):
                try:
                    re.compile(alias)
                except re.error as e:
                    raise forms.ValidationError(
                        _('Invalid alias regexp: {}: {}').format(alias, e.msg))
            else:
                try:
                    validate_email(alias)
                except ValidationError:
                    raise forms.ValidationError(
                        _('Invalid alias email: {}').format(alias))
        return self.cleaned_data['acceptable_aliases']


class DigestSettingsForm(ListSettingsForm):
    """
    List digest settings.
    """
    digests_enabled = forms.ChoiceField(
        choices=((True, _('Yes')), (False, _('No'))),
        widget=forms.RadioSelect,
        required=False,
        label=_('Enable Digests'),
        help_text=_('Should Mailman enable digests for this MailingList?'),
        )
    digest_send_periodic = forms.ChoiceField(
        choices=((True, _('Yes')), (False, _('No'))),
        widget=forms.RadioSelect,
        required=False,
        label=_('Send Digest Periodically'),
        help_text=_('Should Mailman send out digests periodically?'),
        )
    digest_volume_frequency = forms.ChoiceField(
        choices=DIGEST_FREQUENCY_CHOICES,
        widget=forms.RadioSelect,
        required=False,
        label=_('Digest Volume Frequency'),
        help_text=_('At what frequency should Mailman increment the digest '
                    'volume number and reset the issue number?'),
        )
    digest_size_threshold = forms.DecimalField(
        label=_('Digest size threshold'),
        help_text=_('How big in Kb should a digest be before '
                    'it gets sent out?'))


class DMARCMitigationsForm(ListSettingsForm):
    """
    DMARC Mitigations list settings.
    """
    dmarc_mitigate_action = forms.ChoiceField(
        label=_('DMARC mitigation action'),
        widget=forms.Select(),
        required=False,
        error_messages={
            'required': _("Please choose a DMARC mitigation action.")},
        choices=(
            ('no_mitigation', _('No DMARC mitigations')),
            ('munge_from', _('Replace From: with list address')),
            ('wrap_message',
             _('Wrap the message in an outer message From: the list.')),
            ('reject', _('Reject the message')),
            ('discard', _('Discard the message'))),
        help_text=_(
            'The action to apply to messages From: a domain publishing a '
            'DMARC policy of reject or quarantine or to all messages if '
            'DMARC Mitigate unconditionally is True.'))
    dmarc_mitigate_unconditionally = forms.ChoiceField(
        choices=((True, _('Yes')), (False, _('No'))),
        widget=forms.RadioSelect,
        required=False,
        label=_('DMARC Mitigate unconditionally'),
        help_text=_(
            'If DMARC mitigation action is munge_from or wrap_message, '
            'should it apply to all messages regardless of the DMARC policy '
            'of the From: domain.'))
    dmarc_moderation_notice = forms.CharField(
        label=_('DMARC rejection notice'),
        required=False,
        widget=forms.Textarea(),
        help_text=_(
            'Text to replace the default reason in any rejection notice to '
            'be sent when DMARC mitigation action of reject applies.'))
    dmarc_wrapped_message_text = forms.CharField(
        label=_('DMARC wrapped message text'),
        required=False,
        widget=forms.Textarea(),
        help_text=_(
            'Text to be added as a separate text/plain MIME part preceding '
            'the original message part in the wrapped message when DMARC '
            'mitigation action of wrap message applies.'))


PERSONALIZATION_CHOICES = (
    ('none', _('None')),
    ('individual', _('Individual')),
    ('full', _('Full'))
)

PERSONALIZATION_CHOICES_HELP = _(
    """
    None: No personalization.

    Individual: Everyone gets a unique copy of the message, and there are a \
          few more substitution variables, but no headers are modified.

    Full: All of the 'individual' personalization plus recipient header \
          modification. """)

FILTER_ACTION_CHOICES = (
    ('discard', _('Discard')),
    ('reject', _('Reject')),
    ('forward', _('Forward')),
    ('preserve', _('Preserve')),
)

FILTER_ACTION_HELP = _("""Action to take on messages which have no content
after filtering.
 Discard = silently discard the message.
 Reject = discard the message and notify the sender.
 Forward = forward the message to the list owner(s).
 Preserve = save the message in qfiles/bad.
""")


class AlterMessagesForm(ListSettingsForm):
    """
    Alter messages list settings.
    """
    personalize = forms.ChoiceField(
        choices=PERSONALIZATION_CHOICES,
        widget=forms.RadioSelect,
        required=False,
        label=_('Personalize'),
        help_text=PERSONALIZATION_CHOICES_HELP)
    filter_content = forms.ChoiceField(
        choices=((True, _('Yes')), (False, _('No'))),
        widget=forms.RadioSelect,
        required=False,
        label=_('Filter content'),
        help_text=_('Should Mailman filter the content of list traffic '
                    'according to the settings below?'))
    filter_types = ListOfStringsField(
        label=_('Filter types'),
        required=False,
        help_text=_(
            'MIME types to filter from the incoming posts. A list of common '
            'types can be found '
            '<a href="https://developer.mozilla.org/en-US/docs/Web/HTTP/Basics_of_HTTP/MIME_types/Common_types">here </a>'  # noqa# E501
            ))
    filter_extensions = ListOfStringsField(
        label=_('Filter extensions'),
        required=False,
        help_text=_(
            'Extensions to filter from the incoming posts.'
            ))
    pass_types = ListOfStringsField(
        label=_('Pass types'),
        required=False,
        help_text=_(
            'MIME types to allow in the incoming posts. A list of common '
            'types can be found '
            '<a href="https://developer.mozilla.org/en-US/docs/Web/HTTP/Basics_of_HTTP/MIME_types/Common_types">here </a>'  # noqa# E501
            ))
    pass_extensions = ListOfStringsField(
        label=_('Pass extensions'),
        required=False,
        help_text=_(
            'Extensions to allow in the incoming posts.'
            ))
    collapse_alternatives = forms.ChoiceField(
        choices=((True, _('Yes')), (False, _('No'))),
        widget=forms.RadioSelect,
        required=False,
        label=_('Collapse alternatives'),
        help_text=_('Should Mailman collapse multipart/alternative to '
                    'its first part content?'))
    filter_action = forms.ChoiceField(
        choices=FILTER_ACTION_CHOICES,
        widget=forms.RadioSelect,
        required=False,
        label=_('Filter Action'),
        help_text=FILTER_ACTION_HELP)
    convert_html_to_plaintext = forms.ChoiceField(
        choices=((True, _('Yes')), (False, _('No'))),
        widget=forms.RadioSelect,
        required=False,
        label=_('Convert html to plaintext'),
        help_text=_('Should Mailman convert text/html parts to plain text? '
                    'This conversion happens after MIME attachments '
                    'have been stripped.'))
    anonymous_list = forms.ChoiceField(
        choices=((True, _('Yes')), (False, _('No'))),
        widget=forms.RadioSelect,
        required=False,
        label=_('Anonymous list'),
        help_text=_('Hide the sender of a message, '
                    'replacing it with the list address '
                    '(Removes From, Sender and Reply-To fields)'))
    include_rfc2369_headers = forms.ChoiceField(
        choices=((True, _('Yes')), (False, _('No'))),
        widget=forms.RadioSelect,
        required=False,
        label=_('Include RFC2369 headers'),
        help_text=_(
            'Yes is highly recommended. RFC 2369 defines a set of List-* '
            'headers that are normally added to every message sent to the '
            'list membership. These greatly aid end-users who are using '
            'standards compliant mail readers. They should normally always '
            'be enabled. However, not all mail readers are standards '
            'compliant yet, and if you have a large number of members who are '
            'using non-compliant mail readers, they may be annoyed at these '
            'headers. You should first try to educate your members as to why '
            'these headers exist, and how to hide them in their mail clients. '
            'As a last resort you can disable these headers, but this is not '
            'recommended (and in fact, your ability to disable these headers '
            'may eventually go away).'))
    allow_list_posts = forms.ChoiceField(
        choices=((True, _('Yes')), (False, _('No'))),
        widget=forms.RadioSelect,
        required=False,
        label=_("Include the list post header"),
        help_text=_(
            "This can be set to no for announce lists that do not wish to "
            "include the List-Post header because posting to the list is "
            "discouraged."))
    reply_to_address = forms.CharField(
        label=_('Explicit reply-to address'),
        required=False,
        help_text=_(
            'This option allows admins to set an explicit Reply-to address. '
            'It is only used if the reply-to is set to use an explicitly set '
            'header'))
    first_strip_reply_to = forms.ChoiceField(
        choices=((True, _('Yes')), (False, _('No'))),
        widget=forms.RadioSelect,
        required=False,
        help_text=_(
            'Should any existing Reply-To: header found in the original '
            'message be stripped? If so, this will be done regardless of '
            'whether an explict Reply-To: header is added by Mailman or not.'))
    reply_goes_to_list = forms.ChoiceField(
        label=_('Reply goes to list'),
        widget=forms.Select(),
        required=False,
        error_messages={
            'required': _("Please choose a reply-to action.")},
        choices=(
            ('no_munging', _('No Munging')),
            ('point_to_list', _('Reply goes to list')),
            ('explicit_header', _('Explicit Reply-to header set')),
            ('explicit_header_only', _('Explicit Reply-to set; no Cc added'))),
        help_text=_(
            'Where are replies to list messages directed? No Munging is '
            'strongly recommended for most mailing lists. \nThis option '
            'controls what Mailman does to the Reply-To: header in messages '
            'flowing through this mailing list. When set to No Munging, no '
            'Reply-To: header is '
            'added by Mailman, although if one is present in the original '
            'message, it is not stripped. Setting this value to either Reply '
            'to List, Explicit Reply, or Reply Only causes Mailman to insert '
            'a specific Reply-To: header in all messages, overriding the '
            'header in the original message if necessary '
            '(Explicit Reply inserts the value of reply_to_address). '
            'Explicit Reply-to set; no Cc added is useful for'
            'announce-only lists where you want to avoid someone replying '
            'to the list address. There are many reasons not to introduce or '
            'override the Reply-To: header. One is that some posters depend '
            'on their own Reply-To: settings to convey their valid return '
            'address. Another is that modifying Reply-To: makes it much more '
            'difficult to send private replies. See <a href="'
            'http://marc.merlins.org/netrants/reply-to-harmful.html">'
            '`Reply-To\' Munging Considered Harmful</a> for a general '
            'discussion of this issue. See <a href="'
            'http://marc.merlins.org/netrants/reply-to-useful.html">'
            '`Reply-To\' Munging Considered Useful</a> for a dissenting '
            'opinion. '
            'Some mailing lists have restricted '
            'posting privileges, with a parallel list devoted to discussions. '
            'Examples are `patches\' or `checkin\' lists, where software '
            'changes are posted by a revision control system, but discussion '
            'about the changes occurs on a developers mailing list. To '
            'support these types of mailing lists, select Explicit Reply and '
            'set the Reply-To: address option to point to the parallel list.'))
    posting_pipeline = forms.ChoiceField(
        label=_('Pipeline'),
        widget=forms.Select(),
        required=False,
        choices=lambda: ((p, p) for p in get_mailman_client()
                         .pipelines['pipelines']),
        help_text=_('Type of pipeline you want to use for this mailing list'))


class ListAutomaticResponsesForm(ListSettingsForm):
    """
    List settings for automatic responses.
    """
    autorespond_choices = (
        ("respond_and_continue", _("Respond and continue processing")),
        ("respond_and_discard", _("Respond and discard message")),
        ("none", _("No automatic response")))
    autorespond_owner = forms.ChoiceField(
        choices=autorespond_choices,
        widget=forms.RadioSelect,
        label=_('Autorespond to list owner'),
        help_text=_('Should Mailman send an auto-response to '
                    'emails sent to the -owner address?'))
    autoresponse_owner_text = forms.CharField(
        label=_('Autoresponse owner text'),
        widget=forms.Textarea(),
        required=False,
        help_text=_('Auto-response text to send to -owner emails.'))
    autorespond_postings = forms.ChoiceField(
        choices=autorespond_choices,
        widget=forms.RadioSelect,
        label=_('Autorespond postings'),
        help_text=_('Should Mailman send an auto-response to '
                    'mailing list posters?'))
    autoresponse_postings_text = forms.CharField(
        label=_('Autoresponse postings text'),
        widget=forms.Textarea(),
        required=False,
        help_text=_('Auto-response text to send to mailing list posters.'))
    autorespond_requests = forms.ChoiceField(
        choices=autorespond_choices,
        widget=forms.RadioSelect,
        label=_('Autorespond requests'),
        help_text=_(
            'Should Mailman send an auto-response to emails sent to the '
            '-request address? If you choose yes, decide whether you want '
            'Mailman to discard the original email, or forward it on to the '
            'system as a normal mail command.'))
    autoresponse_request_text = forms.CharField(
        label=_('Autoresponse request text'),
        widget=forms.Textarea(),
        required=False,
        help_text=_('Auto-response text to send to -request emails.'))
    autoresponse_grace_period = forms.CharField(
        label=_('Autoresponse grace period'),
        help_text=_(
            'Number of days between auto-responses to either the mailing list '
            'or -request/-owner address from the same poster. Set to zero '
            '(or negative) for no grace period (i.e. auto-respond to every '
            'message).'))
    respond_to_post_requests = forms.ChoiceField(
        choices=((True, _('Yes')), (False, _('No'))),
        widget=forms.RadioSelect,
        required=False,
        label=_('Notify users of held messages'),
        help_text=_(
            'Should Mailman notify users about their messages held for '
            'approval. If you say \'No\', no notifications will be sent '
            'to users about the pending approval on their messages.'))
    send_welcome_message = forms.ChoiceField(
        choices=((True, _('Yes')), (False, _('No'))),
        widget=forms.RadioSelect,
        required=False,
        label=_('Send welcome message'),
        help_text=_(
            'Send welcome message to newly subscribed members? '
            'Turn this off only if you plan on subscribing people manually '
            'and don\'t want them to know that you did so. Setting this to No '
            'is most useful for transparently migrating lists from some other '
            'mailing list manager to Mailman.\n'
            'The text of Welcome message can be set via the Templates tab.'))
    send_goodbye_message = forms.ChoiceField(
        choices=((True, _('Yes')), (False, _('No'))),
        widget=forms.RadioSelect,
        required=False,
        label=_('Send goodbye message'),
        help_text=_(
            'Send goodbye message to newly unsubscribed members? '
            'Turn this off only if you plan on unsubscribing people manually '
            'and don\'t want them to know that you did so.\n'
            'The text of Goodbye message can be set via the Templates tab.'))
    admin_immed_notify = forms.BooleanField(
        widget=forms.RadioSelect(choices=((True, _('Yes')), (False, _('No')))),
        required=False,
        label=_('Admin immed notify'),
        help_text=_(
            'Should the list moderators get immediate notice of new requests, '
            'as well as daily notices about collected ones? List moderators '
            '(and list administrators) are sent daily reminders of requests '
            'pending approval, like subscriptions to a moderated list, '
            'or postings that are being held for one reason or another. '
            'Setting this option causes notices to be sent immediately on the '
            'arrival of new requests as well. '))
    admin_notify_mchanges = forms.BooleanField(
        widget=forms.RadioSelect(choices=((True, _('Yes')), (False, _('No')))),
        required=False,
        label=_('Notify admin of membership changes'),
        help_text=_('Should administrator get notices of '
                    'subscribes and unsubscribes?'))


NEWSGROUP_MODERATION_CHOICES = (
    ('none', _('Not Moderated')),
    ('open_moderated', _('Moderated but allows for open posting')),
    ('moderated', _('Moderated')),
)


class ListIdentityForm(ListSettingsForm):
    """
    List identity settings.
    """
    advertised = forms.ChoiceField(
        choices=((True, _('Yes')), (False, _('No'))),
        widget=forms.RadioSelect,
        label=_('Show list on index page'),
        help_text=_('Choose whether to include this list '
                    'on the list of all lists'))
    description = forms.CharField(
        label=_('Short Description'),
        required=False,
        help_text=_(
            'This description is used when the mailing list is listed with '
            'other mailing lists, or in headers, and so forth. It should be '
            'as succinct as you can get it, while still identifying what the '
            'list is.'),
    )
    info = forms.CharField(
        label=_('Long Description'),
        help_text=_('A longer description of this mailing list.'),
        required=False,
        widget=forms.Textarea())
    display_name = forms.CharField(
        label=_('Display name'),
        required=False,
        help_text=_('Display name is the name shown in the web interface.')
    )
    subject_prefix = forms.CharField(
        label=_('Subject prefix'),
        strip=False,
        required=False,
    )
    preferred_language = forms.ChoiceField(
        label=_('Preferred Language'),
        required=False,
        widget=forms.Select(),
        choices=LANGUAGES,
    )
    member_roster_visibility = forms.ChoiceField(
        label=_('Members List Visibility'),
        required=False,
        widget=forms.Select(),
        choices=ROSTER_VISIBILITY_CHOICES,
        help_text=_('Who is allowed to see members list for this MailingList?')
    )
    gateway_to_mail = forms.BooleanField(
        widget=forms.RadioSelect(choices=((True, _('Yes')), (False, _('No')))),
        required=False,
        label=_('Gateway to mail'),
        help_text=_('Flag indicating that posts to the linked newsgroup should'
                    ' be gated to the list')
    )
    gateway_to_news = forms.BooleanField(
        widget=forms.RadioSelect(choices=((True, _('Yes')), (False, _('No')))),
        required=False,
        label=_('Gateway to news'),
        help_text=_('Flag indicating that posts to the list should be gated to'
                    ' the linked newsgroup.')
    )
    linked_newsgroup = forms.CharField(
        label=_('Linked Newsgroup'),
        required=False,
        help_text=_(
            'The name of the linked newsgroup.')
    )
    newsgroup_moderation = forms.ChoiceField(
        label=_('Newsgroup moderation'),
        required=False,
        widget=forms.Select(),
        choices=NEWSGROUP_MODERATION_CHOICES,
        help_text=_('The moderation policy for the linked newsgroup,'
                    ' if there is one.')
    )
    nntp_prefix_subject_too = forms.BooleanField(
        widget=forms.RadioSelect(choices=((True, _('Yes')), (False, _('No')))),
        required=False,
        label=_('NNTP Include subject prefix '),
        help_text=_('Flag indicating whether the list\'s "Subject Prefix"'
                    ' should be included in posts gated to usenet.')
    )

    def clean_subject_prefix(self):
        """
        Strip the leading whitespaces from the subject_prefix form field.
        """
        return self.cleaned_data.get('subject_prefix', '').lstrip()


class ListMassSubscription(forms.Form):
    """Form fields to masssubscribe users to a list.
    """
    emails = ListOfStringsField(
        label=_('Emails to mass subscribe'),
        help_text=_(
            'The following formats are accepted:\n'
            'jdoe@example.com\n'
            '&lt;jdoe@example.com&gt;\n'
            'John Doe &lt;jdoe@example.com&gt;\n'
            '"John Doe" &lt;jdoe@example.com&gt;\n'
            'jdoe@example.com (John Doe)\n'
            'Use the last three to associate a display name with the address\n'
            ),
    )

    pre_confirmed = forms.BooleanField(
        label=_('Pre confirm'),
        initial=True,
        required=False,
        help_text=_(
            'If checked, users will not have to confirm their subscription.'),
        widget=forms.CheckboxInput()
        )

    pre_approved = forms.BooleanField(
        label=_('Pre approved'),
        initial=True,
        required=False,
        help_text=_(
            'If checked, moderators will not have to approve the subscription'
            ' request.',),
        widget=forms.CheckboxInput()
        )

    pre_verified = forms.BooleanField(
        label=_('Pre Verified'),
        initial=False,
        required=False,
        help_text=_(
            'If checked, users will not have to verify that their '
            'email address is valid.'),
        widget=forms.CheckboxInput()
        )

    invitation = forms.BooleanField(
        label=_('Invitation'),
        initial=False,
        required=False,
        help_text=_(
            'If checked, the other checkboxes are ignored and the users will '
            'be sent an invitation to join the list and will be subscribed '
            'upon acceptance thereof.'),
        widget=forms.CheckboxInput()
        )

    send_welcome_message = forms.ChoiceField(
        choices=((True, _('Yes')),
                 (False, _('No')),
                 ('default', _('List default'))),
        widget=forms.RadioSelect,
        initial='default',
        required=False,
        label=_('Send welcome message'),
        help_text=_(
            'If set to "Yes" or "No", List\'s default setting of '
            'send_welcome_message will be ignored for these subscribers and a'
            ' welcome message will be sent or not sent based on the choice.'),
        )

    def clean_send_welcome_message(self):
        """Choose from True or False. Any other value is equivalent to None."""
        data = self.cleaned_data['send_welcome_message']
        if data in ('True', 'False'):
            return data
        # None implies this value is unset and isn't passed on to Core in API
        # call.
        return None


class ListMassRemoval(forms.Form):

    """Form fields to remove multiple list users.
    """
    emails = ListOfStringsField(
        label=_('Emails to Unsubscribe'),
        help_text=_('Add one email address on each line'),
    )

    class Meta:

        """
        Class to define the name of the fieldsets and what should be
        included in each.
        """
        layout = [["Mass Removal", "emails"]]


class ListHeaderMatchForm(forms.Form):
    """Edit a list's header match."""

    HM_ACTION_CHOICES = [(None, _("Default antispam action"))] + \
                        [a for a in ACTION_CHOICES if a[0] != 'defer']

    header = forms.CharField(
        label=_('Header'),
        help_text=_('Email header to filter on (case-insensitive).'),
        error_messages={
            'required': _('Please enter a header.'),
            'invalid': _('Please enter a valid header.')})
    pattern = forms.CharField(
        label=_('Pattern'),
        help_text=_('Regular expression matching the header\'s value.'),
        error_messages={
            'required': _('Please enter a pattern.'),
            'invalid': _('Please enter a valid pattern.')})
    action = forms.ChoiceField(
        label=_('Action'),
        error_messages={'invalid': _('Please enter a valid action.')},
        required=False,
        choices=HM_ACTION_CHOICES,
        help_text=_('Action to take when a header matches')
    )


class ListHeaderMatchFormset(forms.BaseFormSet):
    def clean(self):
        """Checks that no two header matches have the same order."""
        if any(self.errors):
            # Don't bother validating the formset unless
            # each form is valid on its own
            return
        orders = []
        for form in self.forms:
            try:
                order = form.cleaned_data['ORDER']
            except KeyError:
                continue
            if order in orders:
                raise forms.ValidationError('Header matches must have'
                                            ' distinct orders.')
            orders.append(order)


class MemberModeration(forms.Form):
    """
    Form handling the member's moderation_action.
    """
    moderation_action = moderation_action_field()


class ChangeSubscriptionForm(forms.Form):

    subscriber = forms.ChoiceField(
        label=_('Select Email'),
        required=False,
        widget=forms.Select(),
        validators=[validate_uuid_or_email, ],)

    member_id = forms.CharField(
        required=True,
        label="",
        widget=forms.TextInput(attrs={'readonly': True, 'hidden': True}))

    def __init__(self, user_emails, user_id, primary_email,
                 *args, **kwargs):
        super(ChangeSubscriptionForm, self).__init__(*args, **kwargs)
        choices = list((address, address)
                       for address in user_emails)
        if primary_email and user_id:
            choices.insert(
                0,
                (user_id, _('Primary Address ({})').format(primary_email)))
        self.fields['subscriber'].choices = choices


class TemplateUpdateForm(forms.ModelForm):
    data = forms.CharField(
        label=_('Data'),
        required=False,
        strip=False,
        widget=forms.Textarea(),
        help_text=_email_template_help_text)

    class Meta:
        model = EmailTemplate
        fields = ['data']


class TokenConfirmForm(forms.Form):
    """Form to confirm pending (un)subscription requests from User."""
    token = forms.CharField(
        required=True,
        label="",
        widget=forms.TextInput(attrs={'readonly': True, 'hidden': True}))
