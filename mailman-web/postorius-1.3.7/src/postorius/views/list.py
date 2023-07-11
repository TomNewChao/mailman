# -*- coding: utf-8 -*-
# Copyright (C) 1998-2022 by the Free Software Foundation, Inc.
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

import re
import csv
import email.utils
import logging
import sys
import traceback
import urllib
from urllib.error import HTTPError
from django.core.cache import cache
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.forms import formset_factory
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods

from allauth.account.models import EmailAddress
from django_mailman3.lib.mailman import (
    get_mailman_client, get_mailman_user, get_mailman_user_id)
from django_mailman3.lib.paginator import MailmanPaginator, paginate
from django_mailman3.models import MailDomain
from django_mailman3.signals import (
    mailinglist_created, mailinglist_deleted, mailinglist_modified)

from postorius.auth.decorators import (
    list_moderator_required, list_owner_required, superuser_required)
from postorius.auth.mixins import ListOwnerMixin
from postorius.forms import (
    AlterMessagesForm, ArchiveSettingsForm, BounceProcessingForm,
    ChangeSubscriptionForm, DigestSettingsForm, DMARCMitigationsForm,
    ListAnonymousSubscribe, ListAutomaticResponsesForm, ListHeaderMatchForm,
    ListHeaderMatchFormset, ListIdentityForm, ListMassRemoval,
    ListMassSubscription, ListNew, ListSubscribe, MemberForm, MemberModeration,
    MemberPolicyForm, MessageAcceptanceForm, MultipleChoiceForm,
    UserPreferences, ListAnonymousUnsubscribe)
from postorius.forms.fields import (
    DELIVERY_MODE_CHOICES, DELIVERY_STATUS_CHOICES)
from postorius.forms.list_forms import ACTION_CHOICES, TokenConfirmForm
from postorius.models import (
    Domain, List, Mailman404Error, Style, SubscriptionMode)
from postorius.utils import get_member_or_nonmember, set_preferred
from postorius.views.generic import MailingListView, bans_view
from postorius.verification_email import UnsubscribeEmailImp

logger = logging.getLogger(__name__)


#: DeliveryStatus field values that an admin cannot set.
DISABLED_DELIVERY_STATUS_CHOICES_ADMIN = ['by_bounces']

DELIVERY_MODE_DICT = dict(DELIVERY_MODE_CHOICES)
DELIVERY_STATUS_DICT = dict(DELIVERY_STATUS_CHOICES)


class TokenOwner:
    """Who 'owns' the token returned from the registrar?"""
    subscriber = 'subscriber'
    moderator = 'moderator'


class ListMembersViews(ListOwnerMixin, MailingListView):

    # List of allowed roles for the memberships. The string value matches the
    # exact value Core's REST API expects.
    allowed_roles = ['owner', 'moderator', 'member', 'nonmember']

    def _prepare_query(self, request):
        """Prepare regex based query to search partial email addresses.

        Core's `members/find` API allows searching for memberships based on
        regex. This methods prepares a valid regex to pass on the REST API.
        """
        if request.GET.get('q'):
            query = request.GET['q']
            if "*" not in query:
                query = '*{}*'.format(query)
        else:
            query = ''

        return query

    def get(self, request, list_id, role):
        """Handle GET for Member view.

        This includes all the membership roles (self.allowed_roles).
        """
        member_form = MemberForm()
        # If the role is misspelled, redirect to the default subscribers.
        if role not in self.allowed_roles:
            return redirect('list_members', list_id, 'member')
        context = dict()
        context['list'] = self.mailing_list
        # Warning: role not translatable
        context['role'] = role
        context['member_form'] = member_form
        context['page_title'] = _('List {}s'.format(role.capitalize()))
        context['query'] = self._prepare_query(request)

        def find_method(count, page):
            return self.mailing_list.find_members(
                context['query'], role=role, count=count, page=page)

        context['members'] = paginate(
            find_method,
            request.GET.get('page', 1),
            request.GET.get('count', 25),
            paginator_class=MailmanPaginator)
        context['page_subtitle'] = '({})'.format(
            context['members'].object_list.total_size)
        context['form_action'] = _('Add {}'.format(role))
        if context['query']:
            context['empty_error'] = _(
                'No {}s were found matching the search.'.format(role))
        else:
            context['empty_error'] = _('List has no {}s'.format(role))

        return render(request, 'postorius/lists/members.html', context)

    def _member_post(self, request, role):
        """Handle POST for members. Unsubscribe all the members selected."""
        form = MultipleChoiceForm(request.POST)
        if form.is_valid():
            members = form.cleaned_data['choices']
            for member in members:
                self.mailing_list.unsubscribe(member)
            messages.success(
                request,
                _('The selected members have been unsubscribed'))
        return redirect('list_members', self.mailing_list.list_id, role)

    def _non_member_post(self, request, role):
        """Handle POST for membership roles owner, moderator and non-member.

        Add memberships if the form is valid otherwise redirect to list_members
        page with an error message.
        """
        member_form = MemberForm(request.POST)
        if member_form.is_valid():
            try:
                self.mailing_list.add_role(
                    role=role,
                    address=member_form.cleaned_data['email'],
                    display_name=member_form.cleaned_data['display_name'])
                messages.success(
                    request,
                    _('{email} has been added with the role {role}'.format(
                        email=member_form.cleaned_data['email'], role=role)))
            except HTTPError as e:
                messages.error(request, e.msg)
        else:
            messages.error(request, member_form.errors)
        return redirect('list_members', self.mailing_list.list_id, role)

    def post(self, request, list_id, role=None):
        """Handle POST for list members page.

        List members page have more than one forms, depending on the membership
        type.

        - Regular subscribers have a MultipleChoiceForm, which returns a list
          of emails that needs to be unsubscribed from the MailingList. This is
          handled by :func:`self._member_post` method.

        - Owners, moderators and non-members have a MemberForm which allows
          adding new members with the given roles. This is handled by
          :func:`self._non_member_post` method.

        """
        if role not in self.allowed_roles:
            return redirect('list_members', list_id, 'member')

        if role in ('member', ):
            return self._member_post(request, role)
        else:
            return self._non_member_post(request, role)


@login_required
@list_owner_required
def list_member_options(request, list_id, email):
    template_name = 'postorius/lists/memberoptions.html'
    mm_list = List.objects.get_or_404(fqdn_listname=list_id)
    # If the role is specified explicitly, use that, otherwise the value is
    # None and is equivalent to the value not being specified and the lookup
    # happens using only email. This can cause issues when the email is
    # subscribed as both a Non-Member and Owner/Moderator returning the wrong
    # Member object.
    role = request.GET.get('role')
    try:
        mm_member = mm_list.find_members(address=email, role=role)[0]
        member_prefs = mm_member.preferences
    except (ValueError, IndexError):
        raise Http404(_('Member does not exist'))
    except Mailman404Error:
        return render(request, template_name, {'nolists': 'true'})

    initial_moderation = dict([
        (key, getattr(mm_member, key)) for key in MemberModeration.base_fields
        ])

    if request.method == 'POST':
        if request.POST.get("formname") == 'preferences':
            preferences_form = UserPreferences(
                request.POST, preferences=member_prefs,
                disabled_delivery_choices=DISABLED_DELIVERY_STATUS_CHOICES_ADMIN)  # noqa:E501
            if preferences_form.is_valid():
                try:
                    preferences_form.save()
                except HTTPError as e:
                    messages.error(request, e.msg)
                else:
                    messages.success(request, _("The member's preferences"
                                                " have been updated."))
            return redirect('list_member_options', list_id, email)
        elif request.POST.get("formname") == 'moderation':
            moderation_form = MemberModeration(
                request.POST, initial=initial_moderation)
            if moderation_form.is_valid():
                if not moderation_form.has_changed():
                    messages.info(
                        request, _("No change to the member's moderation."))
                    return redirect('list_member_options', list_id, email)
                for key in list(moderation_form.fields.keys()):
                    # In general, it would be a very bad idea to loop over the
                    # fields and try to set them one by one, However,
                    # moderation form has only one field.
                    setattr(mm_member, key, moderation_form.cleaned_data[key])
                try:
                    mm_member.save()
                except HTTPError as e:
                    messages.error(request, e.msg)
                else:
                    messages.success(request, _("The member's moderation "
                                                "settings have been updated."))
                return redirect('list_member_options', list_id, email)
    else:
        preferences_form = UserPreferences(
            preferences=member_prefs,
            disabled_delivery_choices=DISABLED_DELIVERY_STATUS_CHOICES_ADMIN)
        moderation_form = MemberModeration(initial=initial_moderation)
    return render(request, template_name, {
        'mm_member': mm_member,
        'list': mm_list,
        'preferences_form': preferences_form,
        'moderation_form': moderation_form,
        })


class ListSummaryView(MailingListView):
    """Shows common list metrics.
    """

    def get(self, request, list_id):
        data = {'list': self.mailing_list,
                'user_subscribed': False,
                'subscribed_address': None,
                'subscribed_preferred': False,
                'public_archive': False,
                'hyperkitty_enabled': False}
        if self.mailing_list.settings['archive_policy'] == 'public':
            data['public_archive'] = True
        if (getattr(settings, 'TESTING', False) and               # noqa: W504
                'hyperkitty' not in settings.INSTALLED_APPS):
            # avoid systematic test failure when HyperKitty is installed
            # (missing VCR request, see below).
            list(self.mailing_list.archivers)
        archivers = self.mailing_list.archivers
        if ('hyperkitty' in settings.INSTALLED_APPS and          # noqa: W504
                'hyperkitty' in archivers and                    # noqa: W504
                archivers['hyperkitty']):                       # noqa: W504
            data['hyperkitty_enabled'] = True
        if request.user.is_authenticated:
            user_emails = EmailAddress.objects.filter(
                user=request.user, verified=True).order_by(
                "email").values_list("email", flat=True)
            pending_requests = [r['email'] for r in self.mailing_list.requests]

            subscriptions = []
            for address in user_emails:
                if address in pending_requests:
                    data['user_request_pending'] = True
                    continue
                try:
                    member = self.mailing_list.get_member(address)
                except ValueError:
                    pass
                else:
                    subscriptions.append({
                        'subscribed_address': address,
                        'subscriber': member.member_id,
                        'subscribed_preferred': bool(
                            member.subscription_mode ==
                            SubscriptionMode.as_user.name),
                        'delivery_mode': DELIVERY_MODE_DICT.get(
                            member.preferences.get('delivery_mode')),
                        'delivery_status': DELIVERY_STATUS_DICT.get(
                            member.preferences.get('delivery_status'))
                    })
            data['user_subscriptions'] = subscriptions
            data['user_subscribed'] = bool(subscriptions)

            mm_user = get_mailman_user(request.user)
            primary_email = None
            if mm_user.preferred_address is None:
                primary_email = set_preferred(request.user, mm_user)
            else:
                primary_email = mm_user.preferred_address.email
            data['subscribe_form'] = ListSubscribe(
                user_emails, user_id=mm_user.user_id,
                primary_email=primary_email)
        else:
            user_emails = None
            data['anonymous_subscription_form'] = ListAnonymousSubscribe()
            data['anonymous_unsubscription_form'] = ListAnonymousUnsubscribe()
        return render(request, 'postorius/lists/summary.html', data)


class ChangeSubscriptionView(MailingListView):
    """Change mailing list subscription"""

    @staticmethod
    def _is_email(subscriber):
        """Is the subscriber an email address or uuid."""
        return '@' in subscriber

    def _change_subscription(self, member, subscriber):
        """Switch subscriptions to a new email/user.

        :param member: The currently subscriber Member.
        :param subscriber: New email or user_id to switch subscription to.
        """
        # If the Membership is via preferred address and we are switching to an
        # email or vice versa, we have to do the subscribe-unsubscribe dance.
        # If the subscription is via an address, then we can simply PATCH the
        # address in the Member resource.
        if (member.subscription_mode == SubscriptionMode.as_address.name and
                self._is_email(subscriber)):
            member.address = subscriber
            member.save()
            return
        # Keep a copy of the preferences so we can restore them after the
        # Member switches addresses.
        if member.preferences:
            prefs = member.preferences
        member.unsubscribe()
        # Unless the subscriber is an an email and it is not-verified, we
        # should get a Member resource here as response. We should never get to
        # here with subscriber being an un-verified email.
        new_member = self.mailing_list.subscribe(
            subscriber,
            # Since the email is already verified in Postorius.
            pre_confirmed=True,
            # Since this user was already a Member, simply switching Email
            # addresses shouldn't require another approval.
            pre_approved=True)
        self._copy_preferences(new_member.preferences, prefs)

    def _copy_preferences(self, member_pref, old_member_pref):
        """Copy the preferences of the old member to new one.

        :param member_pref: The new member's preference to copy preference to.
        :param old_member_pref: The old_member's preferences to copy
            preferences from.
        """
        # We can't simply switch the Preferences object, so we copy values from
        # previous one to the new one.
        for prop in old_member_pref._properties:
            val = old_member_pref.get(prop)
            if val:
                member_pref[prop] = val
        member_pref.save()

    def _get_membership(self, mm_user, member_id):
        """Get current memberships of the Mailman user."""
        for sub in mm_user.subscriptions:
            if sub.member_id == member_id:
                return sub
        return None

    def _is_subscribed(self, member, subscriber, mm_user):
        """Check if the member is same as the subscriber.

        If they are not:
        - If subscriber is 'user_id' check if member's user_id is same as
           subscriber.
        - if subscriber is address, check if member's address is same as
           subscriber.
        """
        return (
            (member.subscription_mode == SubscriptionMode.as_address.name and
             member.email == subscriber) or
            (member.subscription_mode == SubscriptionMode.as_user.name and
             member.user.user_id == subscriber))

    @method_decorator(login_required)
    def post(self, request, list_id):
        try:
            user_emails = EmailAddress.objects.filter(
                user=request.user, verified=True).order_by(
                "email").values_list("email", flat=True)
            mm_user = get_mailman_user(request.user)
            primary_email = None
            if mm_user.preferred_address is not None:
                primary_email = mm_user.preferred_address.email
            form = ChangeSubscriptionForm(
                user_emails, mm_user.user_id, primary_email, request.POST)

            if form.is_valid():
                # Step 1: Get the source membership user wants to switch from.
                member = self._get_membership(
                    mm_user, form.cleaned_data['member_id'])
                if member is None:
                    # Source membership doesn't exist, use should use subscribe
                    # form instead.
                    messages.error(_('You are not subscribed to {}.').format(
                        self.mailing_list.fqdn_listname))
                    # Not sure what the right place to return to here, since
                    # there is no on_get resource here. Return to
                    # per-subscription-preferences where the user can choose
                    # subscription to change.
                    return redirect('user_subscription_preferences')
                subscriber = form.cleaned_data['subscriber']
                # Step 2: Check if the source and destination subscriber are
                # the same. This means, if the subscriber is a user_id, check
                # they match and if the subscriber is a email address, check if
                # they match.
                if self._is_subscribed(member, subscriber, mm_user):
                    messages.error(request, _('You are already subscribed'))
                else:
                    self._change_subscription(member, subscriber)
                    # If the subscriber is user_id (i.e. not an email
                    # address), Show 'Primary Address ()' in the success
                    # message instead of weird user id.
                    if not self._is_email(subscriber):
                        subscriber = _(
                            'Primary Address ({})').format(primary_email)
                    messages.success(
                        request,
                        _('Subscription changed to {}').format(subscriber))
            else:
                messages.error(
                    request,
                    _('Something went wrong. Please try again. {} {}').format(
                        form.errors, form.fields['subscriber'].choices))
        except HTTPError as e:
            messages.error(request, e.msg)
        return redirect('list_summary', self.mailing_list.list_id)


class ListSubscribeView(MailingListView):
    """
    view name: `list_subscribe`
    """

    @method_decorator(login_required)
    def post(self, request, list_id):
        """
        Subscribes an email address to a mailing list via POST and
        redirects to the `list_summary` view.
        """
        try:
            user_emails = EmailAddress.objects.filter(
                user=request.user, verified=True).order_by(
                "email").values_list("email", flat=True)
            mm_user = get_mailman_user(request.user)
            primary_email = None
            if mm_user.preferred_address is not None:
                primary_email = mm_user.preferred_address.email
            form = ListSubscribe(
                user_emails, mm_user.user_id, primary_email, request.POST)
            if form.is_valid():
                subscriber = form.cleaned_data.get('subscriber')
                display_name = form.cleaned_data.get('display_name')
                delivery_mode = form.cleaned_data.get('delivery_mode')
                delivery_status = form.cleaned_data.get('delivery_status')
                response = self.mailing_list.subscribe(
                    subscriber, display_name,
                    pre_verified=True, pre_confirmed=True,
                    delivery_mode=delivery_mode,
                    delivery_status=delivery_status)
                if (type(response) == dict and                   # noqa: W504
                        response.get('token_owner') == TokenOwner.moderator):
                    messages.success(
                        request, _('Your subscription request has been'
                                   ' submitted and is waiting for moderator'
                                   ' approval.'))
                else:
                    messages.success(request,
                                     _('You are subscribed to %s.') %
                                     self.mailing_list.fqdn_listname)
            else:
                messages.error(request,
                               _('Something went wrong. Please try again.'))
        except HTTPError as e:
            messages.error(request, e.msg)
        return redirect('list_summary', self.mailing_list.list_id)


class ListAnonymousSubscribeView(MailingListView):
    """
    view name: `list_anonymous_subscribe`
    """

    def post(self, request, list_id):
        """
        Subscribes an email address to a mailing list via POST and
        redirects to the `list_summary` view.
        This view is used for unauthenticated users and asks Mailman core to
        verify the supplied email address.
        """
        try:
            form = ListAnonymousSubscribe(request.POST)
            if form.is_valid():
                email = form.cleaned_data.get('email')
                display_name = form.cleaned_data.get('display_name')
                self.mailing_list.subscribe(
                    email, display_name,
                    pre_verified=False, pre_confirmed=False)
                messages.success(request, _('Please check your inbox for further instructions,Or you have already subscribed successfully.'))
            else:
                messages.error(request, _('The email you entered is invalid.'))
        except HTTPError as e:
            messages.success(request, _('Please check your inbox for further instructions,Or you have already subscribed successfully.'))
        return redirect('list_summary', self.mailing_list.list_id)


class AnonymousUnsubscribeEmailView(MailingListView):
    # noinspection PyShadowingNames
    def post(self, request, *args, **kwargs):
        email = request.POST['email']
        # Verify the user actually controls this email, should
        # return 1 if the user owns the email, 0 otherwise.
        pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        if re.match(pattern, email) is None:
            messages.error(request, _('The email you entered is invalid.'))
            return redirect('list_summary', self.mailing_list.list_id)
        try:
            client = get_mailman_client()
            client.get_user(email)
        except urllib.error.HTTPError as e:
            logger.error("e:{}, traceback:{}".format(e.code, traceback.format_exc()))
            messages.success(request, _('Please check your inbox for further instructions, Or the unsubscription request already pending.'))
            return redirect('list_summary', self.mailing_list.list_id)
        if self._has_pending_unsub_req(email):
            messages.success(request, _('Please check your inbox for further instructions, Or the unsubscription request already pending.'))
            return redirect('list_summary', self.mailing_list.list_id)
        try:
            # 1.check pending
            key = "unsubscribe_{}_{}".format(self.mailing_list.list_id, email)
            key = key.lower()
            if cache.has_key(key):
                messages.success(request, _('Please check your inbox for further instructions, Or the unsubscription request already pending.'))
                return redirect('list_summary', self.mailing_list.list_id)
            # 2.get token
            token = UnsubscribeEmailImp.get_uuid()
            # 3.send email
            UnsubscribeEmailImp.send_email(email, self.mailing_list.list_id, token)
            # 4.set pending
            cache.set(key, "1", settings.VERIFICATION_CODE_EXPIRATION)
            UnsubscribeEmailImp.set_token(email, self.mailing_list.list_id, token)
            messages.success(request, _('Please check your inbox for further instructions, Or the unsubscription request already pending.'))
        except Exception as e:
            logger.error("e:{}, traceback:{}".format(e, traceback.format_exc()))
            messages.error(request, _('Failed to send email'))
        return redirect('list_summary', self.mailing_list.list_id)


class AnonymousUnsubscribeView(MailingListView):
    def get(self, request, *args, **kwargs):
        token = request.GET.get("token")
        if token is None:
            messages.error(request, _('Invalid Link.'))
            return redirect('list_summary', self.mailing_list.list_id)
        try:
            dict_data = UnsubscribeEmailImp.get_token(token)
        except Exception as e:
            logger.error("[AnonymousUnsubscribeView] e:{}".format(str(e)))
            messages.error(request, _('The token expired or invalid.'))
            return redirect('list_summary', self.mailing_list.list_id)
        if "email" not in dict_data.keys() or "list_id" not in dict_data.keys():
            messages.error(request, _('The token expired or invalid.'))
            return redirect('list_summary', self.mailing_list.list_id)
        key = "unsubscribe_{}_{}".format(self.mailing_list.list_id, dict_data['email'])
        key = key.lower()
        if not cache.has_key(key):
            messages.error(request, _('The token expired or invalid.'))
            return redirect('list_summary', self.mailing_list.list_id)
        try:
            email_str = dict_data["email"]
            response = self.mailing_list.unsubscribe(email_str)
            if response is not None and response.get('token') is not None:
                messages.success(
                    request, _('Your unsubscription request has been'
                               ' submitted and is waiting for moderator'
                               ' approval.'))
            else:
                messages.success(request, _('%s has been unsubscribed'
                                            ' from this list.') % dict_data["email"])
        except ValueError as e:
            messages.error(request, e)
        finally:
            UnsubscribeEmailImp.delete_key(key)
            UnsubscribeEmailImp.delete_key(token, is_token=True)
        return redirect('list_summary', self.mailing_list.list_id)


class ListUnsubscribeView(MailingListView):

    """Unsubscribe from a mailing list."""

    @method_decorator(login_required)
    def post(self, request, *args, **kwargs):
        email = request.POST['email']
        # Verify the user actually controls this email, should
        # return 1 if the user owns the email, 0 otherwise.
        found_email = EmailAddress.objects.filter(
            user=request.user, email=email, verified=True).count()
        if found_email == 0:
            messages.error(
                request,
                _('You can only unsubscribe yourself.'))
            return redirect('list_summary', self.mailing_list.list_id)
        if self._has_pending_unsub_req(email):
            messages.error(
                request,
                _('You have a pending unsubscription request waiting for'
                  ' moderator approval.'))
            return redirect('list_summary', self.mailing_list.list_id)
        try:
            response = self.mailing_list.unsubscribe(email)
            if response is not None and response.get('token') is not None:
                messages.success(
                    request, _('Your unsubscription request has been'
                               ' submitted and is waiting for moderator'
                               ' approval.'))
            else:
                messages.success(request, _('%s has been unsubscribed'
                                            ' from this list.') % email)
        except ValueError as e:
            messages.error(request, e)
        return redirect('list_summary', self.mailing_list.list_id)


@login_required
@list_owner_required
def list_mass_subscribe(request, list_id):
    mailing_list = List.objects.get_or_404(fqdn_listname=list_id)
    if request.method == 'POST':
        form = ListMassSubscription(request.POST)
        if form.is_valid():
            for data in form.cleaned_data['emails']:
                try:
                    # Parse the data to get the address and the display name
                    display_name, address = email.utils.parseaddr(data)
                    validate_email(address)
                    p_v = form.cleaned_data['pre_verified']
                    p_c = form.cleaned_data['pre_confirmed']
                    p_a = form.cleaned_data['pre_approved']
                    inv = form.cleaned_data['invitation']
                    mailing_list.subscribe(
                        address=address,
                        display_name=display_name,
                        pre_verified=p_v,
                        pre_confirmed=p_c,
                        pre_approved=p_a,
                        invitation=inv,
                        send_welcome_message=form.cleaned_data[
                            'send_welcome_message'])
                    if inv:
                        message = _('The address %(address)s has been'
                                    ' invited to %(list)s.')
                    else:
                        message = _('The address %(address)s has been'
                                    ' subscribed to %(list)s.')
                        if not (p_v and p_c and p_a):
                            message += _(' Subscription may be pending address'
                                         ' verification, confirmation or'
                                         ' approval as appropriate.')
                    messages.success(
                        request, message %
                        {'address': address,
                         'list': mailing_list.fqdn_listname})
                except HTTPError as e:
                    messages.error(request, e)
                except ValidationError:
                    messages.error(request, _('The email address %s'
                                              ' is not valid.') % address)
                    # When the email address is bad, set the focus back to the
                    # email field after returning to the same page.
                    form.fields['emails'].widget.attrs['autofocus'] = True
    else:
        form = ListMassSubscription()
    return render(request, 'postorius/lists/mass_subscribe.html',
                  {'form': form, 'list': mailing_list})


class ListMassRemovalView(MailingListView):
    """Class For Mass Removal"""

    @method_decorator(login_required)
    @method_decorator(list_owner_required)
    def get(self, request, *args, **kwargs):
        form = ListMassRemoval()
        return render(request, 'postorius/lists/mass_removal.html',
                      {'form': form, 'list': self.mailing_list})

    @method_decorator(list_owner_required)
    def post(self, request, *args, **kwargs):
        form = ListMassRemoval(request.POST)
        if not form.is_valid():
            messages.error(request, _('Please fill out the form correctly.'))
        else:
            valid_emails = []
            invalid_emails = []
            for data in form.cleaned_data['emails']:
                try:
                    # Parse the data to get the address.
                    address = email.utils.parseaddr(data)[1]
                    validate_email(address)
                    valid_emails.append(address)
                except ValidationError:
                    invalid_emails.append(data)

            try:
                self.mailing_list.mass_unsubscribe(valid_emails)
                messages.success(
                    request, _('These addresses {address} have been'
                               ' unsubscribed from {list}.'.format(
                                   address=', '.join(valid_emails),
                                   list=self.mailing_list.fqdn_listname)))
            except (HTTPError, ValueError) as e:
                messages.error(request, e)

            if len(invalid_emails) > 0:
                messages.error(
                    request,
                    _('The email address %s is not valid.') % invalid_emails)
        return redirect('mass_removal', self.mailing_list.list_id)


def _perform_action(message_ids, action):
    for message_id in message_ids:
        action(message_id)


@login_required
@list_moderator_required
def list_moderation(request, list_id, held_id=-1):
    mailing_list = List.objects.get_or_404(fqdn_listname=list_id)
    if request.method == 'POST':
        form = MultipleChoiceForm(request.POST)
        if form.is_valid():
            message_ids = form.cleaned_data['choices']
            try:
                if 'accept' in request.POST:
                    _perform_action(message_ids, mailing_list.accept_message)
                    messages.success(request,
                                     _('The selected messages were accepted'))
                elif 'reject' in request.POST:
                    _perform_action(message_ids, mailing_list.reject_message)
                    messages.success(request,
                                     _('The selected messages were rejected'))
                elif 'discard' in request.POST:
                    _perform_action(message_ids, mailing_list.discard_message)
                    messages.success(request,
                                     _('The selected messages were discarded'))
            except HTTPError:
                messages.error(request, _('Message could not be found'))
    else:
        form = MultipleChoiceForm()
    held_messages = paginate(
        mailing_list.get_held_page,
        request.GET.get('page'), request.GET.get('count'),
        paginator_class=MailmanPaginator)
    context = {
        'list': mailing_list,
        'held_messages': held_messages,
        'form': form,
        'ACTION_CHOICES': ACTION_CHOICES,
        }
    return render(request, 'postorius/lists/held_messages.html', context)


@require_http_methods(['POST'])
@login_required
@list_moderator_required
def moderate_held_message(request, list_id):
    """Moderate one held message"""
    mailing_list = List.objects.get_or_404(fqdn_listname=list_id)
    msg = mailing_list.get_held_message(request.POST['msgid'])
    moderation_choice = request.POST.get('moderation_choice')
    reason = request.POST.get('reason')

    try:
        if 'accept' in request.POST:
            mailing_list.accept_message(msg.request_id)
            messages.success(request, _('The message was accepted'))
        elif 'reject' in request.POST:
            mailing_list.reject_message(msg.request_id, reason=reason)
            messages.success(request, _('The message was rejected'))
        elif 'discard' in request.POST:
            mailing_list.discard_message(msg.request_id)
            messages.success(request, _('The message was discarded'))
    except HTTPError as e:
        if e.code == 404:
            messages.error(
                request,
                _('Held message was not found.'))
            return redirect('list_held_messages', list_id)
        else:
            raise

    moderation_choices = dict(ACTION_CHOICES)
    if moderation_choice in moderation_choices:
        member = get_member_or_nonmember(mailing_list, msg.sender)
        if member is not None:
            member.moderation_action = moderation_choice
            member.save()
            messages.success(
                request,
                _('Moderation action for {} set to {}'.format(
                    member, moderation_choices[moderation_choice])))
        else:
            messages.error(
                request,
                _('Failed to set moderation action for {}'.format(msg.sender)))
    return redirect('list_held_messages', list_id)


@login_required
@list_owner_required
def csv_view(request, list_id):
    """Export all the subscriber in csv
    """
    mm_lists = List.objects.get_or_404(fqdn_listname=list_id)

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = (
        'attachment; filename="Subscribers.csv"')

    writer = csv.writer(response)
    if mm_lists:
        for i in mm_lists.members:
            writer.writerow([i.email])

    return response


def _get_choosable_domains(request):
    domains = Domain.objects.all()
    return [(d.mail_host, d.mail_host) for d in domains]


def _get_choosable_styles(request):
    styles = Style.objects.all()
    options = [(style['name'], style['description'])
               for style in styles['styles']]
    return options


def _get_default_style():
    return Style.objects.all()['default']


@login_required
@superuser_required
def list_new(request, template='postorius/lists/new.html'):
    """
    Add a new mailing list.
    If the request to the function is a GET request an empty form for
    creating a new list will be displayed. If the request method is
    POST the form will be evaluated. If the form is considered
    correct the list gets created and otherwise the form with the data
    filled in before the last POST request is returned. The user must
    be logged in to create a new list.
    """
    mailing_list = None
    choosable_domains = [('', _('Choose a Domain'))]
    choosable_domains += _get_choosable_domains(request)
    choosable_styles = _get_choosable_styles(request)
    if request.method == 'POST':
        form = ListNew(choosable_domains, choosable_styles, request.POST)
        if form.is_valid():
            # grab domain
            domain = Domain.objects.get_or_404(
                mail_host=form.cleaned_data['mail_host'])
            # creating the list
            try:
                mailing_list = domain.create_list(
                    form.cleaned_data['listname'],
                    style_name=form.cleaned_data['list_style'])
                mailing_list.add_owner(form.cleaned_data['list_owner'])
                list_settings = mailing_list.settings
                if form.cleaned_data['description']:
                    list_settings["description"] = \
                        form.cleaned_data['description']
                list_settings["advertised"] = form.cleaned_data['advertised']
                list_settings.save()
                messages.success(request, _("List created"))
                mailinglist_created.send(sender=List,
                                         list_id=mailing_list.list_id)
                return redirect("list_summary",
                                list_id=mailing_list.list_id)
            # TODO catch correct Error class:
            except HTTPError as e:
                # Right now, there is no good way to detect that this is a
                # duplicate mailing list request other than checking the
                # reason for 400 error.
                if e.reason == 'Mailing list exists':
                    form.add_error(
                        'listname', _('Mailing List already exists.'))
                    return render(request, template, {'form': form})
                # Otherwise just render the generic error page.
                return render(request, 'postorius/errors/generic.html',
                              {'error': e.msg})
        else:
            messages.error(request, _('Please check the errors below'))
    else:
        form = ListNew(choosable_domains, choosable_styles, initial={
            'list_owner': request.user.email,
            'advertised': True,
            'list_style': _get_default_style(),
            })
    return render(request, template, {'form': form})


def _unique_lists(lists):
    """Return unique lists from a list of mailing lists."""
    return {mlist.list_id: mlist for mlist in lists}.values()


def _get_mail_host(web_host):
    """Get the mail_host for a web_host if FILTER_VHOST is true and there's
    only one mail_host for this web_host.
    """
    if not getattr(settings, 'FILTER_VHOST', False):
        return None
    mail_hosts = []
    use_web_host = False
    for domain in Domain.objects.all():
        try:
            if (MailDomain.objects.get(
                    mail_domain=domain.mail_host).site.domain == web_host):
                if domain.mail_host not in mail_hosts:
                    mail_hosts.append(domain.mail_host)
        except MailDomain.DoesNotExist:
            use_web_host = True
    if len(mail_hosts) == 1:
        return mail_hosts[0]
    elif len(mail_hosts) == 0 and use_web_host:
        return web_host
    else:
        return None


@login_required
def list_index_authenticated(request):
    """Index page for authenticated users.

    Index page for authenticated users is slightly different than
    un-authenticated ones. Authenticated users will see all their memberships
    in the index page.

    This view is not paginated and will show all the lists.

    """
    role = request.GET.get('role', None)
    client = get_mailman_client()
    choosable_domains = _get_choosable_domains(request)

    # Get the user_id of the current user
    user_id = get_mailman_user_id(request.user)

    mail_host = _get_mail_host(request.get_host().split(':')[0])
    # Get all the mailing lists for the current user.
    try:
        all_lists = client.find_lists(user_id,
                                      role=role,
                                      mail_host=mail_host,
                                      count=sys.maxsize)
    except HTTPError:
        # No lists exist with the given role for the given user.
        all_lists = []
    # If the user has no list that they are subscriber/owner/moderator of, we
    # just redirect them to the index page with all lists.
    if len(all_lists) == 0 and role is None:
        return redirect(reverse('list_index') + '?all-lists')
    # Render the list index page with `check_advertised = False` since we don't
    # need to check for advertised list given that all the users are related
    # and know about the existence of the list anyway.
    context = {
        'lists': _unique_lists(all_lists),
        'domain_count': len(choosable_domains),
        'role': role,
        'check_advertised': False,
    }
    return render(
        request,
        'postorius/index.html',
        context
    )


def list_index(request, template='postorius/index.html'):
    """Show a table of all public mailing lists."""
    # TODO maxking: Figure out why does this view accept POST request and why
    # can't it be just a GET with list parameter.
    if request.method == 'POST':
        return redirect("list_summary", list_id=request.POST["list"])
    # If the user is logged-in, show them only related lists in the index,
    # except role is present in requests.GET.
    if request.user.is_authenticated and 'all-lists' not in request.GET:
        return list_index_authenticated(request)

    def _get_list_page(count, page):
        client = get_mailman_client()
        advertised = not request.user.is_superuser
        mail_host = _get_mail_host(request.get_host().split(":")[0])
        return client.get_list_page(
            advertised=advertised, mail_host=mail_host, count=count, page=page)

    lists = paginate(
        _get_list_page, request.GET.get('page'), request.GET.get('count'),
        paginator_class=MailmanPaginator)

    # This is just an optimization to skip un-necessary API
    # calls. postorius/index.html page shows the 'Create New Domain' button
    # when the logged-in user is a super user. There is no point making those
    # API calls if the user isn't a Superuser. So, just call the number 0 if
    # the user isn't SU.
    if request.user.is_superuser:
        domain_count = len(_get_choosable_domains(request))
    else:
        domain_count = 0

    return render(request, template,
                  {'lists': lists,
                   'check_advertised': True,
                   'all_lists': True,
                   'domain_count': domain_count})


def all_list_index(request):
    """get all list info"""
    from django.http import JsonResponse, HttpResponseForbidden
    if request.method == "GET":
        client = get_mailman_client()
        mailman_lists = client.get_lists(advertised=True)
        ret_list = list()
        for mailman_obj in mailman_lists:
            mailman_info = {key: getattr(mailman_obj, key) for key in mailman_obj._properties}
            ret_list.append(mailman_info)
        return JsonResponse(data=ret_list, safe=False)
    else:
        return HttpResponseForbidden()


@login_required
@list_owner_required
def list_delete(request, list_id):
    """Deletes a list but asks for confirmation first.
    """
    the_list = List.objects.get_or_404(fqdn_listname=list_id)
    if request.method == 'POST':
        the_list.delete()
        mailinglist_deleted.send(sender=List, list_id=list_id)
        return redirect("list_index")
    else:
        submit_url = reverse('list_delete',
                             kwargs={'list_id': list_id})
        cancel_url = reverse('list_index',)
        return render(request, 'postorius/lists/confirm_delete.html',
                      {'submit_url': submit_url, 'cancel_url': cancel_url,
                       'list': the_list})


@login_required
@list_moderator_required
def list_pending_confirmations(request, list_id):
    """Shows a list of subscription requests.
    """
    return _list_subscriptions(
        request=request,
        list_id=list_id,
        token_owner=TokenOwner.subscriber,
        template='postorius/lists/pending_confirmations.html',
        page_title=_('Subscriptions pending user confirmation'),
        )


@login_required
@list_moderator_required
def list_subscription_requests(request, list_id):
    """Shows a list of subscription requests."""
    return _list_subscriptions(
        request=request,
        list_id=list_id,
        token_owner=TokenOwner.moderator,
        template='postorius/lists/subscription_requests.html',
        page_title=_('Subscriptions pending approval'),
        )


@login_required
@list_moderator_required
def list_unsubscription_requests(request, list_id):
    """Shows a list of pending unsubscription requests."""
    return _list_subscriptions(
        request=request,
        list_id=list_id,
        token_owner=TokenOwner.moderator,
        template='postorius/lists/subscription_requests.html',
        page_title=_('Un-subscriptions pending approval'),
        request_type='unsubscription'
        )


def _list_subscriptions(
        request, list_id, token_owner, template, page_title,
        request_type='subscription'):
    m_list = List.objects.get_or_404(fqdn_listname=list_id)
    requests = [req
                for req in m_list.get_requests(
                    token_owner=token_owner, request_type=request_type)]
    paginated_requests = paginate(
        requests,
        request.GET.get('page'),
        request.GET.get('count', 25))
    page_subtitle = '(%d)' % len(requests)
    return render(request, template,
                  {'list': m_list,
                   'paginated_requests': paginated_requests,
                   'page_title': page_title,
                   'page_subtitle': page_subtitle})


@login_required
@list_moderator_required
@require_http_methods(['POST'])
def handle_subscription_request(request, list_id, request_id):
    """
    Handle a subscription request. Possible actions:
        - accept
        - defer
        - reject
        - discard
    """
    confirmation_messages = {
        'accept': _('The request has been accepted.'),
        'reject': _('The request has been rejected.'),
        'discard': _('The request has been discarded.'),
        'defer': _('The request has been defered.'),
    }
    # Get the action by the presence of one of the values in POST data.
    for each in confirmation_messages:
        if each in request.POST:
            action = each
    # If None of the actions were specified, just raise an error and return.
    m_list = List.objects.get_or_404(fqdn_listname=list_id)
    if action is None:
        messages.error(f'Invalid or missing action {action!r}')
        return redirect('list_subscription_requests', m_list.list_id)
    reason = request.POST.get('reason')
    try:
        # Moderate request and add feedback message to session.
        m_list.moderate_request(request_id, action, reason)
        messages.success(request, confirmation_messages[action])
    except HTTPError as e:
        if e.code == 409:
            messages.success(request,
                             _('The request was already moderated: %s')
                             % e.reason)
        else:
            messages.error(request, _('The request could not be moderated: %s')
                           % e.reason)
    return redirect('list_subscription_requests', m_list.list_id)


SETTINGS_SECTION_NAMES = (
    ('list_identity', gettext_lazy('List Identity')),
    ('automatic_responses', gettext_lazy('Automatic Responses')),
    ('alter_messages', gettext_lazy('Alter Messages')),
    ('dmarc_mitigations', gettext_lazy('DMARC Mitigations')),
    ('digest', gettext_lazy('Digest')),
    ('message_acceptance', gettext_lazy('Message Acceptance')),
    ('archiving', gettext_lazy('Archiving')),
    ('subscription_policy', gettext_lazy('Member Policy')),
    ('bounce_processing', gettext_lazy('Bounce Processing')),
)

SETTINGS_FORMS = {
    'list_identity': ListIdentityForm,
    'automatic_responses': ListAutomaticResponsesForm,
    'alter_messages': AlterMessagesForm,
    'dmarc_mitigations': DMARCMitigationsForm,
    'digest': DigestSettingsForm,
    'message_acceptance': MessageAcceptanceForm,
    'archiving': ArchiveSettingsForm,
    'subscription_policy': MemberPolicyForm,
    'bounce_processing': BounceProcessingForm,
}


@login_required
@list_owner_required
def list_settings(request, list_id=None, visible_section=None,
                  template='postorius/lists/settings.html'):
    """
    View and edit the settings of a list.
    The function requires the user to be logged in and have the
    permissions necessary to perform the action.

    Use /<NAMEOFTHESECTION>/<NAMEOFTHEOPTION>
    to show only parts of the settings
    <param> is optional / is used to differ in between section and option might
    result in using //option
    """
    if visible_section is None:
        visible_section = 'list_identity'
    try:
        form_class = SETTINGS_FORMS[visible_section]
    except KeyError:
        raise Http404('No such settings section')
    m_list = List.objects.get_or_404(fqdn_listname=list_id)
    list_settings = m_list.settings
    initial_data = dict((key, value) for key, value in list_settings.items())
    # List settings are grouped and processed in different forms.
    if request.method == 'POST':
        form = form_class(request.POST, mlist=m_list, initial=initial_data)
        if form.is_valid():
            try:
                for key in form.changed_data:
                    if key in form_class.mlist_properties:
                        setattr(m_list, key, form.cleaned_data[key])
                    else:
                        val = form.cleaned_data[key]
                        # Empty list isn't really a valid value and Core
                        # interprets empty string as empty value for
                        # ListOfStringsField. We are doing it here instead of
                        # ListOfStringsField.to_python() because output from
                        # list of strings is expected to be a list and the NULL
                        # value is hence an empty list. The serialization of
                        # the empty list is for us empty string, hence we are
                        # doing this here. Technically, it can be done outside
                        # of this view, but there aren't any other use cases
                        # where we'd want an empty list of strings.
                        if val == []:
                            val = ''
                        list_settings[key] = val
                list_settings.save()
                messages.success(request, _('The settings have been updated.'))
                mailinglist_modified.send(sender=List, list_id=m_list.list_id)
            except HTTPError as e:
                messages.error(
                    request,
                    _('An error occurred: ') + e.reason)
            return redirect('list_settings', m_list.list_id, visible_section)
    else:
        form = form_class(initial=initial_data, mlist=m_list)

    return render(request, template, {
        'form': form,
        'section_names': SETTINGS_SECTION_NAMES,
        'list': m_list,
        'visible_section': visible_section,
        })


@login_required
@list_owner_required
def remove_role(request, list_id=None, role=None, address=None,
                template='postorius/lists/confirm_remove_role.html'):
    """Removes a list moderator or owner."""
    the_list = List.objects.get_or_404(fqdn_listname=list_id)
    redirect_on_success = redirect('list_members', the_list.list_id, role)
    roster = getattr(the_list, '{}s'.format(role))
    all_emails = [each.email for each in roster]
    if address not in all_emails:
        messages.error(request,
                       _('The user %(email)s is not in the %(role)s group')
                       % {'email': address, 'role': role})
        return redirect('list_members', the_list.list_id, role)

    if role == 'owner':
        if len(roster) == 1:
            messages.error(request, _('Removing the last owner is impossible'))
            return redirect('list_members', the_list.list_id, role)
        user_emails = EmailAddress.objects.filter(
            user=request.user, verified=True).order_by(
            "email").values_list("email", flat=True)
        if address in user_emails:
            # The user is removing themselves, redirect to the list info page
            # because they won't have access to the members page anyway.
            redirect_on_success = redirect('list_summary', the_list.list_id)

    if request.method == 'POST':
        try:
            the_list.remove_role(role, address)
        except HTTPError as e:
            messages.error(request, _('The user could not be removed: %(msg)s')
                           % {'msg': e.msg})
            return redirect('list_members', the_list.list_id, role)
        messages.success(request, _('The user %(address)s has been removed'
                                    ' from the %(role)s group.')
                         % {'address': address, 'role': role})
        return redirect_on_success
    return render(request, template,
                  {'role': role, 'address': address,
                   'list_id': the_list.list_id})


@login_required
@list_owner_required
def remove_all_subscribers(request, list_id):
    """Empty the list by unsubscribing all members."""

    mlist = List.objects.get_or_404(fqdn_listname=list_id)
    if len(mlist.members) == 0:
        messages.error(request,
                       _('No member is subscribed to the list currently.'))
        return redirect('mass_removal', mlist.list_id)
    if request.method == 'POST':
        try:
            # TODO maxking: This doesn't scale. Either the Core should provide
            # an endpoint to remove all subscribers or there should be some
            # better way to do this. Maybe, Core can take a list of email
            # addresses in batches of 50 and unsubscribe all of them.
            for names in mlist.members:
                mlist.unsubscribe(names.email)
            messages.success(request, _('All members have been'
                                        ' unsubscribed from the list.'))
            return redirect('list_members', mlist.list_id, 'subscriber')
        except Exception as e:
            messages.error(request, e)
    return render(request,
                  'postorius/lists/confirm_removeall_subscribers.html',
                  {'list': mlist})


@login_required
@list_owner_required
def list_bans(request, list_id):
    return bans_view(
        request, list_id=list_id, template='postorius/lists/bans.html')


@login_required
@list_owner_required
def list_header_matches(request, list_id):
    """
    View and edit the list's header matches.
    """
    m_list = List.objects.get_or_404(fqdn_listname=list_id)
    header_matches = m_list.header_matches
    HeaderMatchFormset = formset_factory(
        ListHeaderMatchForm, extra=1, can_delete=True, can_order=True,
        formset=ListHeaderMatchFormset)
    initial_data = [
        dict([
            (key, getattr(hm, key)) for key in ListHeaderMatchForm.base_fields
        ]) for hm in header_matches]

    # Process form submission.
    if request.method == 'POST':
        formset = HeaderMatchFormset(request.POST, initial=initial_data)
        if formset.is_valid():
            if not formset.has_changed():
                return redirect('list_header_matches', list_id)
            # Purge the existing header_matches
            header_matches.clear()
            # Add the ones in the form

            def form_order(f):
                # If ORDER is None (new header match), add it last.
                return f.cleaned_data.get('ORDER') or len(formset.forms)
            errors = []
            for form in sorted(formset, key=form_order):
                if 'header' not in form.cleaned_data:
                    # The new header match form was not filled
                    continue
                if form.cleaned_data.get('DELETE'):
                    continue
                try:
                    header_matches.add(
                        header=form.cleaned_data['header'],
                        pattern=form.cleaned_data['pattern'],
                        action=form.cleaned_data['action'],
                        )
                except HTTPError as e:
                    errors.append(e)
            for e in errors:
                messages.error(
                    request, _('An error occurred: %s') % e.reason)
            if not errors:
                messages.success(request, _('The header matches were'
                                            ' successfully modified.'))
            return redirect('list_header_matches', list_id)
    else:
        formset = HeaderMatchFormset(initial=initial_data)
    # Adapt the last form to create new matches
    form_new = formset.forms[-1]
    form_new.fields['header'].widget.attrs['placeholder'] = _('New header')
    form_new.fields['pattern'].widget.attrs['placeholder'] = _('New pattern')
    del form_new.fields['ORDER']
    del form_new.fields['DELETE']

    return render(request, 'postorius/lists/header_matches.html', {
        'list': m_list,
        'formset': formset,
        })


def confirm_token(request, list_id):
    """Confirm token sent via Mailman Core.

    This view allows confirming tokens sent via Mailman Core for confirming
    subscriptions or verifying email addresses. This is an un-authenticated
    view and the authentication is done by assuming the secrecy of the token
    sent.

    :param request: The Django request object.
    :param list_id: The current mailinglist id.

    """
    m_list = List.objects.get_or_404(fqdn_listname=list_id)
    if request.method == 'POST':
        form = TokenConfirmForm(request.POST)
        # If the token is None or something, just raise 400 error.
        if not form.is_valid():
            raise HTTPError(request.path, 400, _('Invalid confirmation token'),
                            None, None)
        token = form.cleaned_data.get('token')
        try:
            pending_req = m_list.get_request(token)
        except HTTPError as e:
            if e.code == 404:
                raise HTTPError(request.path, 404,
                                _('Token expired or invalid.'), None, None)
            raise
        # Since we only accept the token there isn't any need for Form data. We
        # just need a POST request at this URL to accept the token.
        m_list.moderate_request(token, action='accept')
        messages.success(request, _('Mailbox subscription mailing list success.'))
        return redirect('list_summary', m_list.list_id)
    # Get the token from url parameter.
    token = request.GET.get('token')
    try:
        pending_req = m_list.get_request(token)
    except HTTPError as e:
        if e.code == 404:
            raise HTTPError(request.path, 404, _('Token expired or invalid.'),
                            None, None)
        raise

    # If this is a token pending moderator approval, they need to login and
    # approve it from the pending requests page.
    if pending_req.get('token_owner') == 'moderator':
        return redirect('list_subscription_requests', m_list.list_id)

    token_type = pending_req.get('type')
    form = TokenConfirmForm(initial=dict(token=token))
    # Show the display_name if it is not None or "".
    if pending_req.get('display_name'):
        addr = email.utils.formataddr((pending_req.get('display_name'),
                                       pending_req.get('email')))
    else:
        addr = pending_req.get('email')

    return render(request, 'postorius/lists/confirm_token.html', {
        'mlist': m_list,
        'addr': addr,
        'token': token,
        'type': token_type,
        'form': form,
    })
