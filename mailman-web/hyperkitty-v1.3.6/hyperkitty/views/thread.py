# -*- coding: utf-8 -*-
#
# Copyright (C) 2014-2022 by the Free Software Foundation, Inc.
#
# This file is part of HyperKitty.
#
# HyperKitty is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# HyperKitty is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along with
# HyperKitty.  If not, see <http://www.gnu.org/licenses/>.
#
# Author: Aamir Khan <syst3m.w0rm@gmail.com>
# Author: Aurelien Bompard <abompard@fedoraproject.org>
#

import datetime
import json
import re
import logging

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import SuspiciousOperation
from django.db.models import Count
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template import loader
from django.urls import reverse
from django.utils.timezone import utc
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

import robot_detection
from haystack.query import SearchQuerySet

from hyperkitty.forms import AddTagForm, ReplyForm
from hyperkitty.lib.utils import stripped_subject
from hyperkitty.lib.view_helpers import (
    check_mlist_private, get_category_widget, get_months, get_posting_form)
from hyperkitty.models import Favorite, LastView, Tag, Tagging, Thread, Vote


REPLY_RE = re.compile(r'^(re:\s*)*', re.IGNORECASE)

#: GET request parameter that signifies the backend to respond with a page that
#: doesn't require Javascript to function.
NO_SCRIPT = 'noscript'
logger = logging.getLogger(__name__)

def _get_thread_replies(request, thread, limit, offset=0):
    '''
    Get and sort the replies for a thread.
    '''
    if not thread:
        raise Http404

    sort_mode = request.GET.get("sort", "thread")
    if sort_mode not in ("date", "thread"):
        raise SuspiciousOperation
    if sort_mode == "thread":
        sort_mode = "thread_order"

    mlist = request.mlist
    initial_subject = stripped_subject(mlist, thread.starting_email.subject)
    emails = list(thread
                  .emails
                  .exclude(pk=thread.starting_email.pk)
                  .select_related('sender')
                  .annotate(attachments_count=Count('attachments'))
                  .order_by(sort_mode)[offset:offset+limit])
    email_ids = [email.id for email in emails]
    votes = dict(Vote
                 .objects
                 .filter(email_id__in=email_ids, user_id=request.user.id)
                 .values_list('email_id', 'value'))
    for email in emails:
        # Extract all the votes for this message
        if request.user.is_authenticated:
            email.myvote = votes.get(email.id)
        else:
            email.myvote = None
        # Threading position
        if sort_mode == "thread_order":
            # If the email's thread_order is None, we set it to 1 by
            # default. This field should be re-computed when the async job
            # compute_thread_order_depth runs for this new email comes in
            # and fix things.
            if email.thread_order is None:
                email.thread_order = 1
            email.level = email.thread_depth - 1  # replies start ragged left
            if email.level > 5:
                email.level = 5
            elif email.level < 0:
                email.level = 0
        # Subject change
        subject = email.subject.strip()
        if mlist.subject_prefix:
            subject = subject.replace(mlist.subject_prefix, "")
        subject = REPLY_RE.sub("", subject.strip())
        if subject == initial_subject.strip():
            email.changed_subject = False
        else:
            email.changed_subject = subject
    return emails


@check_mlist_private
def thread_index(request, mlist_fqdn, threadid, month=None, year=None):
    ''' Displays all the email for a given thread identifier '''
    # The check_mlist_private decorator sets the .mlist attribute
    # on the request.
    mlist = request.mlist
    thread = get_object_or_404(Thread, mailinglist=mlist, thread_id=threadid)
    # Note(maxking): The query below fetches the thread along with it's
    # starting_email in a single complex query, instead of two rather simple
    # ones. We _could_ use the one below, but with simple tests in dev
    # environment, there isn't a major speed bump that I am seeing, so I am
    # going to keep it commented out.
    #
    # thread = Thread.objects.select_related('starting_email').get(
    #      mailinglist=mlist, thread_id=threadid)

    starting_email = thread.starting_email

    # clean html to text
    def clean_html(html):
        # First we remove inline JavaScript/CSS:
        cleaned = re.sub(r"(?is)<(script|style).*?>.*?(</\1>)", "", html.strip())
        # Then we remove html comments. This has to be done before removing regular
        # tags since comments can contain '>' characters.
        cleaned = re.sub(r"(?s)<!--(.*?)-->[\n]?", "", cleaned)
        # Next we can remove the remaining tags:
        cleaned = re.sub(r"(?s)<.*?>", " ", cleaned)
        # Finally, we deal with whitespace
        cleaned = re.sub(r"&nbsp;", " ", cleaned)
        cleaned = re.sub(r"  ", " ", cleaned)
        return cleaned.strip()

    list_attach = list(filter(lambda x: x.name == "attachment.html" and x.content_type == "text/html", starting_email.attachments.all()))
    if len(list_attach) != 0:
        try:
            email_content = list_attach[0].content.tobytes().decode(list_attach[0].encoding)
            email_text = clean_html(email_content)
            if email_text:
                starting_email.content = email_text
        except Exception as e:
            logger.error("e:{}".format(e))

    sort_mode = request.GET.get("sort", "thread")
    if request.user.is_authenticated:
        myvote = starting_email.votes.filter(user=request.user).first()
        if myvote is not None:
            starting_email.myvote = myvote.value
    else:
        starting_email.myvote = None

    # Tags
    tag_form = AddTagForm()

    # Favorites
    fav_action = "add"
    if request.user.is_authenticated and Favorite.objects.filter(
            thread=thread, user=request.user).exists():
        fav_action = "rm"

    # Category
    # category, category_form = get_category_widget(request, thread.category)

    # Extract relative dates
    today = datetime.date.today()
    days_old = today - starting_email.date.date()
    days_inactive = today - thread.date_active.date()

    subject = stripped_subject(mlist, starting_email.subject)

    # Last view
    last_view = None
    if request.user.is_authenticated:
        last_view_obj, created = LastView.objects.get_or_create(
                thread=thread, user=request.user)
        if not created:
            last_view = last_view_obj.view_date
            last_view_obj.save()  # update timestamp
    # get the number of unread messages
    if last_view is None:
        if request.user.is_authenticated:
            unread_count = thread.emails_count
        else:
            unread_count = 0
    else:
        unread_count = thread.emails.filter(date__gt=last_view).count()

    # TODO: eventually move to a middleware ?
    # http://djangosnippets.org/snippets/1865/
    user_agent = request.META.get('HTTP_USER_AGENT', None)
    if user_agent:
        is_bot = robot_detection.is_robot(user_agent)
    else:
        is_bot = True

    # Export button
    export = {
        "url": "%s?thread=%s" % (
            reverse("hk_list_export_mbox", kwargs={
                    "mlist_fqdn": mlist.name,
                    "filename": "%s-%s" % (mlist.name, thread.thread_id)}),
            thread.thread_id),
        "message": _("Download"),
        "title": _("This thread in gzipped mbox format"),
    }

    is_noscript = NO_SCRIPT in request.GET

    context = {
        'mlist': mlist,
        'thread': thread,
        'starting_email': starting_email,
        'subject': subject,
        'addtag_form': tag_form,
        'month': thread.date_active,
        'months_list': get_months(mlist),
        'days_inactive': days_inactive.days,
        'days_old': days_old.days,
        'sort_mode': sort_mode,
        'fav_action': fav_action,
        'reply_form': get_posting_form(ReplyForm, request, mlist),
        'is_bot': is_bot,
        'is_noscript': is_noscript,
        'num_comments': thread.emails_count - 1,
        'last_view': last_view,
        'unread_count': unread_count,
        # XX(abraj): Commenting it out, since we don't need this. We aren't
        # really using. At some point we can remove this but I am commenting.
        #
        # 'category_form': category_form,
        # 'category': category,
        'export': export,
        'posting_enabled': getattr(
            settings, 'HYPERKITTY_ALLOW_WEB_POSTING', True),
    }

    if is_bot or is_noscript:
        # Don't rely on AJAX to load the replies
        # The limit is a safety measure, don't let a bot kill the DB
        context["replies"] = _get_thread_replies(request, thread, limit=1000)

    return render(request, "hyperkitty/thread.html", context=context)


@check_mlist_private
def replies(request, mlist_fqdn, threadid):
    """Get JSON encoded lists with the replies and the participants"""
    # chunk_size must be an even number, or the even/odd cycle will be broken.
    chunk_size = 6
    offset = int(request.GET.get("offset", "0"))
    # This is set by the @check_mlist_private decorator.
    mlist = request.mlist
    thread = get_object_or_404(Thread, mailinglist=mlist, thread_id=threadid)
    # Last view
    last_view = request.GET.get("last_view")
    if last_view:
        try:
            last_view = datetime.datetime.fromtimestamp(int(last_view), utc)
        except ValueError:
            last_view = None
    context = {
        'mlist': mlist,
        'threadid': thread,
        'reply_form': get_posting_form(ReplyForm, request, mlist),
        'last_view': last_view,
        'posting_enabled': getattr(
            settings, 'HYPERKITTY_ALLOW_WEB_POSTING', True),
    }
    context["replies"] = _get_thread_replies(request, thread, offset=offset,
                                             limit=chunk_size)

    replies_tpl = loader.get_template('hyperkitty/ajax/replies.html')
    replies_html = replies_tpl.render(context, request)
    response = {
        "replies_html": replies_html,
        "more_pending": False,
        "next_offset": None,
        }
    if len(context["replies"]) == chunk_size:
        response["more_pending"] = True
        response["next_offset"] = offset + chunk_size
    return HttpResponse(json.dumps(response),
                        content_type='application/javascript')


@require_POST
@check_mlist_private
def tags(request, mlist_fqdn, threadid):
    """ Add or remove one or more tags on a given thread. """
    if not request.user.is_authenticated:
        return HttpResponse('You must be logged in to add a tag',
                            content_type="text/plain", status=403)
    thread = get_object_or_404(
        Thread, mailinglist=request.mlist, thread_id=threadid)

    action = request.POST.get("action")

    if action == "add":
        form = AddTagForm(request.POST)
        if not form.is_valid():
            return HttpResponse("Error adding tag: invalid data",
                                content_type="text/plain", status=500)
        tagname = form.data['tag']
    elif action == "rm":
        tagname = request.POST.get('tag')
    else:
        raise SuspiciousOperation
    tagnames = [t.strip() for t in re.findall(r"[\w'_ -]+", tagname)]
    for tagname in tagnames:
        if action == "add":
            tag = Tag.objects.get_or_create(name=tagname)[0]
            Tagging.objects.get_or_create(
                tag=tag, thread=thread, user=request.user)
        elif action == "rm":
            try:
                Tagging.objects.get(tag__name=tagname, thread=thread,
                                    user=request.user).delete()
            except Tagging.DoesNotExist:
                raise Http404("No such tag: %s" % tagname)
            # cleanup
            if not Tagging.objects.filter(tag__name=tagname).exists():
                Tag.objects.filter(name=tagname).delete()

    # Now refresh the tag list
    tpl = loader.get_template('hyperkitty/threads/tags.html')
    html = tpl.render({"thread": thread}, request)

    response = {"tags": [t.name for t in thread.tags.distinct()],
                "html": html}
    return HttpResponse(json.dumps(response),
                        content_type='application/javascript')


@check_mlist_private
def suggest_tags(request, mlist_fqdn, threadid):
    term = request.GET.get("term")
    current_tags = Tag.objects.filter(
            threads__mailinglist__name=mlist_fqdn,
            threads__thread_id=threadid
        ).values_list("name", flat=True)
    if term:
        tags_db = Tag.objects.filter(
            threads__mailinglist__name=mlist_fqdn,
            name__istartswith=term)
    else:
        tags_db = Tag.objects.all()

    tag_names = list(tags_db.exclude(
        name__in=current_tags).values_list("name", flat=True)[:20])
    return HttpResponse(json.dumps(tag_names),
                        content_type='application/javascript')


@require_POST
@check_mlist_private
def favorite(request, mlist_fqdn, threadid):
    """ Add or remove from favorites"""
    if not request.user.is_authenticated:
        return HttpResponse('You must be logged in to have favorites',
                            content_type="text/plain", status=403)

    thread = get_object_or_404(
        Thread, mailinglist=request.mlist, thread_id=threadid)
    if request.POST["action"] == "add":
        Favorite.objects.get_or_create(thread=thread, user=request.user)
    elif request.POST["action"] == "rm":
        Favorite.objects.filter(thread=thread, user=request.user).delete()
    else:
        raise SuspiciousOperation
    return HttpResponse("success", content_type='text/plain')


@require_POST
@check_mlist_private
def set_category(request, mlist_fqdn, threadid):
    """ Set the category for a given thread. """
    if not request.user.is_authenticated:
        return HttpResponse('You must be logged in to add a tag',
                            content_type="text/plain", status=403)

    thread = get_object_or_404(
        Thread, mailinglist=request.mlist, thread_id=threadid)
    category, category_form = get_category_widget(request)
    if not category and thread.category:
        thread.category = None
        thread.save()
    elif category and category.name != thread.category:
        thread.category = category
        thread.save()

    # Now refresh the category widget
    context = {
            "category_form": category_form,
            "thread": thread,
            "category": category,
            }
    return render(request, "hyperkitty/threads/category.html", context)


@check_mlist_private
def reattach(request, mlist_fqdn, threadid):
    if not request.user.is_staff and not request.user.is_superuser:
        return HttpResponse('You must be a staff member to reattach a thread',
                            content_type="text/plain", status=403)
    mlist = request.mlist
    thread = get_object_or_404(Thread, mailinglist=mlist, thread_id=threadid)
    context = {
        'mlist': mlist,
        'thread': thread,
        'months_list': get_months(mlist),
    }
    template_name = "hyperkitty/reattach.html"

    if request.method == 'POST':
        parent_tid = request.POST.get("parent")
        if not parent_tid:
            parent_tid = request.POST.get("parent-manual")
        if not parent_tid or not re.match(r"\w{32}", parent_tid):
            messages.warning(
                request,
                "Invalid thread id, it should look "
                "like OUAASTM6GS4E5TEATD6R2VWMULG44NKJ.")
            return render(request, template_name, context)
        if parent_tid == threadid:
            messages.warning(
                request,
                "Can't re-attach a thread to itself, check your thread ID.")
            return render(request, template_name, context)
        try:
            parent = Thread.objects.get(
                mailinglist=thread.mailinglist, thread_id=parent_tid)
        except Thread.DoesNotExist:
            messages.warning(
                request,
                "Unknown thread, check your thread ID.")
            return render(request, template_name, context)
        try:
            thread.starting_email.set_parent(parent.starting_email)
        except ValueError as e:
            messages.error(request, str(e))
            return render(request, template_name, context)
        messages.success(request, "Thread successfully re-attached.")
        return redirect(reverse(
                'hk_thread', kwargs={
                    "mlist_fqdn": mlist_fqdn,
                    'threadid': parent_tid,
                }))
    return render(request, template_name, context)


@check_mlist_private
def reattach_suggest(request, mlist_fqdn, threadid):
    mlist = request.mlist
    thread = get_object_or_404(Thread, mailinglist=mlist, thread_id=threadid)

    default_search_query = stripped_subject(
        mlist, thread.subject).lower().replace("re:", "")
    search_query = request.GET.get("q")
    if not search_query:
        search_query = default_search_query
    search_query = search_query.strip()
    emails = SearchQuerySet().filter(
        mailinglist__exact=mlist_fqdn, content=search_query)[:50]
    suggested_threads = []
    for msg in emails:
        sugg_thread = msg.object.thread
        if (sugg_thread not in suggested_threads and
                sugg_thread.thread_id != threadid):
            suggested_threads.append(sugg_thread)

    context = {
        'mlist': mlist,
        'suggested_threads': suggested_threads[:10],
    }
    return render(request, "hyperkitty/ajax/reattach_suggest.html", context)
