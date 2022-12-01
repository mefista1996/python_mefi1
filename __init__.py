# coding=utf-8
import calendar
import itertools
import json
import os
import re
import sys
import time
import urlparse
from copy import copy
from datetime import timedelta, date, datetime
from operator import methodcaller
from urllib import urlencode
from urlparse import urljoin

import pytz
import requests
from dateparser import parse as dateparser_parse
from dateutil.parser import parse as dateutil_parse
from flask import current_app
from flask_babel import gettext, get_locale, lazy_gettext
from flask_jwt_extended import create_access_token
from jinja2.exceptions import TemplateError
from lxml import etree
from requests.exceptions import RequestException
from rq.utils import enum
from sqlalchemy.exc import DataError

from healthjoy.assessment.constants import ASSESSMENT_SOURCE_MEMBER_INPUT, ASSESSMENT_SOURCE_RX
from healthjoy.assessment.models import UserProfilePCPRecord, UserProfileMedication
from healthjoy.assistant.models import AssistantThread, ThreadStatus
from healthjoy.assistant.utils import (have_online_suitable_pha, _update_thread_status, create_outbound_chat,
                                       prepare_attachments_from_external_files, send_pha_slack_notification)
from healthjoy.auth.constants import U2P_RELATION_SPOUSE, U2P_RELATION_DEPENDENT, U2P_RELATION_ADULT_CHILD, GENDERS
from healthjoy.auth.models import UserProfile, UserProfileSources
from healthjoy.auth.validators import format_phone_number
from healthjoy.constants import COMPANY_ICON_URL_PLACEHOLDER, HJ_ICON_URL
from healthjoy.cplatform.constants import APS_CATEGORY_NOT_CHAT, APS_LINK_CATEGORY, PUSH_CATEGORY_OUTBOUND_JOURNEY
from healthjoy.delivery import DeliveryChannel
from healthjoy.delivery.jobs import send_mobile_push, notify_card_updated_push, notify_outbound_journey_updated_push
from healthjoy.delivery.utils import schedule_push, cancel_scheduled_pushes
from healthjoy.ext import (
    chatbot,
    cmanager_client,
    sentry,
    db,
    log,
    data_api,
    analytics,
    analytics_v2,
    slack,
    redis,
    babel,
    file_manager_storage,
    provider_basket_client,
    service_requests_client,
    med_profile_client,
    geo_service_client,
    telemed_client
)
from healthjoy.ext.base_api_client import ApiError
from healthjoy.ext.chatbot_utils import (ChatBotConnectionError, format_chatbot_message,
                                         message_smart_encode)
from healthjoy.ext.datarover.helpers import DataItemsWrapper, DataAutocompleteWrapper, DataCountWrapper
from healthjoy.icr.dc import get_recommended_providers
from healthjoy.icr.fake_journeys import DEFAULT_TELEMED_FAKE_JOURNEY, ACME_DEMO_JOURNEYS, ACME_DEMO
from healthjoy.icr.journey import (DefaultJourney, TileSelectHandler, BaseJourney, _journeys, JourneyNotFound,
                                   chatbot_command_handler, DECISION_CARD_CLASSES, translate_all)
from healthjoy.icr.models import JourneyState
from healthjoy.icr.models.cards import (ProviderCard, RXCard, MedicalBillReviewCard, FacilityCard, AppointmentCard,
                                        ProviderDecision, MessageCard, FacilityDecision, FindCareProviderCard,
                                        FindCareAppointmentCard)
from healthjoy.icr.utils import invalidate_widgets_cache, AutocloseManager
from healthjoy.insurance.constants import MEDICAL_PLAN_TYPE, INSURANCE_PLAN_TYPES
from healthjoy.management.models import HealthJoyConfigs, MessageText
from healthjoy.mbr.models import MedicalBillReviewAppointmentSlot
from healthjoy.mbr.utils import SlotsManager
from healthjoy.notification import (send_notification, reset_onboarding_demo_user,
                                    publish_communication_event, COMMUNICATION_OPENED)
from healthjoy.notification.utils import add_customerio_attachment
from healthjoy.telemedicine.models import ConsultationQueue, ConsultationQuestionnaire
from healthjoy.utils import ValidationError, smart_str, UserAlreadyExistsValidationerror
from healthjoy.utils.deferred_rq import cancel_job
from healthjoy.utils.dt import get_tz_datetime, is_adult, split_tz_date_string, get_ics_calendar
from healthjoy.utils.helpers import request_data_to_multidict
from healthjoy.utils.i18n import with_user_locale
from healthjoy.utils.mobile_backcompat import user_client_supports_improved_journey_tracking
from healthjoy.utils.provider_basket import prepare_provider
from healthjoy.wallets.workflows import get_user_wallet_cards
from .context import ChatbotContext
from .helpers import (make_select, VALIDATORS, SELECT_TYPE_STATIC_LIST, static_list_registry, ChatControlsController,
                      BrokenChatbotLogic, SELECT_TYPE_MULTISELECT, SELECT_TYPE_HORIZONTAL,
                      parse_chatbot_intake, SELECT_TYPE_BUTTONS)
from .setters import set_chatbot_value
from healthjoy.icr.constants import (PROVIDER_DATA_FIELDS, CHATBOT_LINEBREAK_TOKEN, CAMPAIGN_FAKE_JOURNEYS,
                                     DISPO_SLACK_REPORT_EVENTS, JOURNEY_CARD_STATES, JOURNEY_READY_TO_VIEW_KEY)
from healthjoy.icr.element import (
    SimpleInstanceElement, AutocompleteElement, PhoneInputElement, ZipcodeInputElement,
    HintElement, HeaderParametersElement, DispositionElement,
    ConversationBrowseElement, InputElement, PhotoElement, DateInputElement,
    JourneyPresentationElement, WorkdayInputElement, CheckPermissionElement,
    RequestPermissionElement, GetLocationElement, DisclaimerElement, StripePopupElement,
    PermissionSettingsElement, RefreshWebViewsElement, AnimationSpeedElement, ChatURLElement,
    CloseAppElement, UserJidUpdatedElement, WrongXControlCount, MultipleSelectElement,
    BaseICRElement, ConversationCompletedNotificationElement,
    FileUploadElement, SimpleChoiceElement, Attach, OutboundJourneyStateUpdated, InboxUpdatedNotificationElement)
from ...telemedicine.helpers import get_telemed_url

logger = log.get_logger('journey', setup_elastic=True)
simple_logger = log.get_logger('journey_debug')


def get_decision_center_url(journey):
    if isinstance(journey, JourneyState):
        journey = journey.load_journey()
    return journey and urljoin(current_app.config.get('CRM_DOMAIN'), '{}/{}'.format(journey.decision_center_url_prefix,
                                                                                    journey.id))


class PostponeBotInstructions(StopIteration):
    def __init__(self, delay, *args, **kwargs):
        self.delay = delay
        self.postpone_all = kwargs.pop('_postpone_all', False)
        super(PostponeBotInstructions, self).__init__(*args, **kwargs)

    def __str__(self):
        return '%s (delay: %s)' % (super(PostponeBotInstructions, self).__str__(), self.delay)


class ChatbotSelectHandler(TileSelectHandler):
    def process_tile_selected(self, el, *args, **kwargs):
        solved = super(ChatbotSelectHandler, self).process_tile_selected(el, *args, **kwargs)
        should_close_chat = False
        if isinstance(solved, (list, tuple)):
            solved, should_close_chat = solved
        return solved, should_close_chat

    def handle_free_input(self, el):
        return False

    def handle_default(self, el):
        return False

    def handle_reset_onboard(self, el):
        thread = self.thread
        user = self.user
        user.onboarded = False
        profile = user.get_assistant_profile()
        profile.set_jid_postfix()
        AssistantThread.query.filter_by(profile_id=profile.id).update(dict(deleted=True))

        from healthjoy.icr.widgets import JourneyWidget

        for j in JourneyWidget(user).items():
            db.session.delete(j)

        invalidate_widgets_cache(user.id)
        db.session.commit()
        if user.campaign.is_acme_demo:
            reset_onboarding_demo_user(user.id)

            from healthjoy.auth.jobs import fill_demo_user_ticket
            fill_demo_user_ticket.delay(user.id, eta=timedelta(seconds=5))

        for plan_type in INSURANCE_PLAN_TYPES:
            self.core_benefits.unverify_plan(type_=plan_type, create=False)

        db.session.commit()

        self.journey._chat.send_system_message(thread, UserJidUpdatedElement().render())
        return True, True


class ChatbotBaseJourney(BaseJourney):

    @classmethod
    def get_chatbot_translations(cls):
        return babel.provide_domain('chatbot')

    def __init__(self, state=None, thread=None, parent=None, init_state=None, **kwargs):
        x_controller = kwargs.pop('x_controller', None)
        super(ChatbotBaseJourney, self).__init__(state, thread, parent, init_state, **kwargs)
        self.translations = self.get_chatbot_translations()
        self.x_controller = x_controller or ChatControlsController()
        self._was_thread_bot_tagged = thread and thread.thread_status == ThreadStatus.WithBot

    @classmethod
    def _tricky_parse_qsl(cls, filters):
        if not filters:
            return {}
        if filters.count('=') > filters.count('&'):
            return dict(urlparse.parse_qsl(filters))
        # crutch for cases with symbol "&" in chatbot args
        out = []
        for part in filters.split('='):
            if '&' not in part:
                out.append(part)
                continue

            cur = part.split('&')
            out.extend(reversed([cur.pop(), '&'.join(cur)]))
        out = map(lambda nv: urlparse.unquote(nv.replace('+', ' ')), out)
        return dict(zip(out[::2], out[1::2]))

    @property
    def thread(self):
        thread = super(ChatbotBaseJourney, self).thread
        self._was_thread_bot_tagged = thread.thread_status == ThreadStatus.WithBot
        return thread

    def update_thread_state_as_bot(self, thread=None):
        if thread is None:
            thread = self.thread
        self._was_thread_bot_tagged = True
        was_updated = _update_thread_status(thread, ThreadStatus.WithBot)
        thread.thread_status = ThreadStatus.WithBot
        return was_updated

    def say(self, msg, **kwargs):
        from healthjoy.assistant.backend.jabber_tool_client import JabberToolServiceUnavailable

        is_whisper = kwargs.pop('is_whisper', False)

        try:
            super(ChatbotBaseJourney, self).say(msg, **kwargs)
        except JabberToolServiceUnavailable as e:
            depth_level = self.x_controller.level
            if not depth_level:
                # postpone only alone messages (journey interactors will be postponed in outer scope)
                self._postpone_chatbot_instructions(timedelta(seconds=5), [msg])
            is_update = 'msg_id' in kwargs
            if not is_update:
                raise e

        if is_whisper:
            return

        if self._was_thread_bot_tagged:
            return

        thread = self.thread
        was_with_bot = thread.with_bot
        was_updated = self.update_thread_state_as_bot(thread)
        if was_updated and not was_with_bot:
            self._chat.send_system_message(
                thread,
                ConversationCompletedNotificationElement(thread, force_close_event=True).render()
            )

    def make_and_send_x_control(self, x_control_class, *args, **kwargs):
        journey = self._state
        kwargs.update(
            thread=journey.thread,
            journey=journey
        )
        self.send_x_control(x_control_class(*args, **kwargs))

    def send_x_control(self, x_control):
        self.logger.debug(
            "%s %s adding control from send_x_control: %s",
            self,
            self.x_controller.uid,
            x_control,
        )
        self.x_controller.add_control()
        self.say(x_control, msg_id=x_control.id)

    @property
    def _chatbot_context(self):
        obj = self._extra_context_obj
        return ChatbotContext(self.thread, extra=obj and dict(obj=obj, env=self.env) or None, journey_id=self.id)

    @property
    def _extra_context_obj(self):
        return self.env.get('obj_') or {}

    @chatbot_command_handler('save_context_obj')
    def save_context_obj(self, obj, name='obj_'):
        if isinstance(obj, basestring) and re.match(r'^\d{1,3},((\d{3},)+)?\d{3}$', obj):
            # fix bot digit autoformat (phone number shit)
            obj = obj.replace(',', '')

        self.set_env(name, obj)

    @chatbot_command_handler('refresh_context')
    def _refresh_context(self, key):
        self.set_env(key, None)
        self._delegate_to_chatbot('success')

    @chatbot_command_handler('append_context')
    def _append_context(self, key, text):
        _context = copy(self._state.parameters.get(key) or [])
        _context.append(text)
        self.set_env(key, _context)
        db.session.commit()
        self._delegate_to_chatbot('success')

    def _send_to_bot(self, msg, safe=True):
        user = self.user
        try:
            return chatbot.send_message(user.id, msg, bot_name=user.chatbot, safe=safe)
        except ChatBotConnectionError as e:
            self.logger.warning(u'%s _delegate_to_chatbot failed: %s' % (user.id, e))
            sentry.captureException(sys.exc_info(), tags=dict(bundle=log.extract_bundle(self.user)))

    def _delegate_to_chatbot(self, msg, fallback=None, safe=True, extra_context=None, campaign_type='', meta=None):
        if fallback is None:
            fallback = self._chatbot_fallback
        user = self.thread.assistant_profile.user
        msg = message_smart_encode(msg)

        def warn(msg, *args, **kwargs):
            self.logger.warning(msg, *args, **kwargs)

        with with_user_locale(self.user):

            chatbot_response = self._send_to_bot(msg, safe=safe)

            warn(u'%s _delegate_to_chatbot [contoller: %s] success: %s, response: %s'
                 % (user.id, self.x_controller.uid, msg, chatbot_response))
            if chatbot_response is None or chatbot_response.is_empty:
                warn(u'%s _delegate_to_chatbot empty response' % user.id)
                return fallback()

            try:
                self._execute_chatbot_instructions(chatbot_response,
                                                   extra_context=extra_context,
                                                   campaign_type=campaign_type,
                                                   meta=meta,
                                                   )
            except BrokenChatbotLogic:
                logger.exception('{} Broken bot controls logic (thread: {}, journey: {}, chatbot_response: {})\n'
                                 .format(user.id, self.thread.id, self.id, str(chatbot_response)))
                self.say(WrongXControlCount())
                self._close_chat()

    def _execute_chatbot_instructions(self, chatbot_messages, extra_context=None, campaign_type='', meta=None):

        def _execute_instruction(chatbot_message, **kwargs):
            from healthjoy.assistant.backend.jabber_tool_client import JabberToolServiceUnavailable
            try:
                if isinstance(chatbot_message, BaseICRElement):
                    # got invalid Element proxy on unpickle - so we use our clean elem instance
                    self.say(chatbot_message.render(), campaign_type=kwargs.get('campaign_type', ''), meta=meta)
                elif isinstance(chatbot_message, etree._Element):
                    self.say(chatbot_message, campaign_type=kwargs.get('campaign_type', ''), meta=meta)
                elif isinstance(chatbot_message, basestring) or not chatbot_message.is_system_message:
                    self._say_bot_plain_message(chatbot_message, meta=meta, **kwargs)
                else:
                    self._handle_chatbot_command(chatbot_message.command, *chatbot_message.args, **kwargs)
            except JabberToolServiceUnavailable as e:
                raise PostponeBotInstructions(timedelta(seconds=5), 'Wait for error {} resolving'.format(e),
                                              _postpone_all=True)

        with self.x_controller as x_controller:
            extra_kwargs = dict(extra_context=extra_context, campaign_type=campaign_type)
            for i, m in enumerate(chatbot_messages):
                try:
                    _execute_instruction(m, **extra_kwargs)
                except PostponeBotInstructions as e:
                    delayed_messages = chatbot_messages
                    if not e.postpone_all:
                        delayed_messages = chatbot_messages[i + 1:]  # take unprocessed bot commands
                    self._postpone_chatbot_instructions(e.delay, delayed_messages, **extra_kwargs)
                    x_controller.break_execution()
                    break

    def run_postponed(self, chatbot_messages, extra_context=None, campaign_type=''):
        self.del_from_env('postponed')
        with with_user_locale(self.user):
            self._execute_chatbot_instructions(chatbot_messages, extra_context=extra_context,
                                               campaign_type=campaign_type)

    def cancel_postponed(self):
        postponed_job = self.env.get('postponed')
        if postponed_job:
            cancel_job(postponed_job, connection_name='emma')
        self.del_from_env('postponed')

    def _postpone_chatbot_instructions(self, eta, chatbot_messages, extra_context=None, campaign_type=''):
        from ..jobs import execute_journey_delayed_actions
        self.cancel_postponed()
        job = execute_journey_delayed_actions.delay(
            self.id, chatbot_messages,
            extra_context=extra_context, campaign_type=campaign_type,
            eta=eta
        )
        self.set_env('postponed', job.id)

    def track_journey_step(self, el, step_type):
        analytics.track(self.user, 'journey_step', dict(
            thread_id=self.thread.id,
            topic=self.env.get('journey_topic'),
            value=el.value,
            type=step_type
        ))

    def handle_default(self, msg):
        self._delegate_to_chatbot(msg['body'])

    def _send_photo_display_plain_message(self, el):
        # crutch for sending plain message for displaying photo component in old mobile apps
        assert isinstance(el, PhotoElement)
        self._chat.send_user_message(self.user.jid, self.thread, '', nick=self.user.chat_name,
                                     files=el.attachments)

    def handle_default_submit(self, el):
        self.say(el, msg_id=el.id)
        self.logger.warning(
            'calling handle_default_submit, user: %s. journey: %s, el: %s' % (self.user, self, el)
        )
        self._delegate_to_chatbot(el.chatbot_value)

    def handle_submit_when_autocomplete(self, el):
        show_value = getattr(el, 'show_value', None) or el.value
        result = dict(id=el.value, name=show_value)
        self.say(el, msg_id=el.id)
        self.track_journey_step(el, 'autocomplete')
        self.state = self.env.get('before_autocomplete_state', self.DEFAULT_STATE)
        self.save_context_obj(result)
        db.session.commit()

        self._delegate_to_chatbot(getattr(el, 'show_value_to_delegate', show_value))

    def handle_submit_when_location_info(self, el):
        self.state = self.env.get('before_location_info_state', self.DEFAULT_STATE)
        self.set_env('address_components', el.state.env.get('address_components'))
        db.session.commit()

        return self.handle_default_submit(el)

    def _chatbot_fallback(self):
        self.cancel_postponed()
        self.get_help()
        self.state = self.STOP_STATE

    def _say_bot_plain_message(self, chatbot_message, campaign_type='', extra_context=None, meta=None):
        context = self._chatbot_context
        if extra_context:
            context.update(extra_context)

        translated_message = self.translations.gettext(smart_str(chatbot_message).decode('utf-8'))

        result_message = format_chatbot_message(translated_message, context)
        self.say(result_message, campaign_type=campaign_type, meta=meta)

    def _create_otrs_ticket(self, subject, body, bind_to_journey=True, delay=None, **otrs_kwargs):
        from ..jobs import make_simple_ticket_for_journey
        if self.user.campaign.is_demo_only:
            # No need create OTRS service tickets for demo users
            return

        teams_count = int(HealthJoyConfigs.get_value('OTRS_TICKET_TEAMS_COUNT', default=5))
        team_number = redis.incr('OTRS_TICKET_COUNT') % teams_count + 1
        subject += ' team_%s' % team_number
        make_simple_ticket_for_journey.delay(self.id, subject, body, bind_to_journey=bind_to_journey, eta=delay,
                                             **otrs_kwargs)

    def _get_address_details_for_journey(self, address_components=None):
        """
        Return address data tuple which is required for journey creation.
        If 'address_components' argument not passed, get components from journey env.
        """
        if address_components is None:
            address_components = self.env.get('address_components')
            self.del_from_env('address_components')

        if address_components:
            return (address_components.get("formatted_address"), address_components.get("zip_code"),
                    u'{}, {}'.format(address_components.get("state"), address_components.get("city")))
        else:
            profile = self.user.profile
            return profile.address, profile.zip, u'{}, {}'.format(profile.state, profile.city)

    def _get_from_context(self, key):
        return self.env.get('extra_context', {}).get(key, self._chatbot_context.get(key))

    @chatbot_command_handler('say', translate_args=('chatbot_message',))
    def _say_message_from_chatbot(self, chatbot_message, attachments=None,
                                  save_attachments=False, campaign_type=''):
        if attachments:
            attachments = map(methodcaller('strip'), attachments.split(','))
        if save_attachments:
            attachments = prepare_attachments_from_external_files(
                attachments, self.user, serializable=False
            )
        self.say(chatbot_message, files=attachments, campaign_type=campaign_type)

    @chatbot_command_handler('close_chat')
    def _close_chat(self, *args):
        self.thread.send_close()
        self.x_controller.break_execution()

    @chatbot_command_handler('invite_hcc')
    def _invite_hcc(self, skill=None, force_skill=None):
        skills = skill and skill.split(';') or []
        clean_skills = [s.strip() for s in skills]
        can_ignore_skill = not force_skill

        skills_to_check = [] if can_ignore_skill else clean_skills
        self.cancel_postponed()
        if not have_online_suitable_pha(self.thread, *skills_to_check):
            send_pha_slack_notification.delay(user_profile_url=self.user.houston_profile_url,
                                              skill=skill, can_ignore_skill=not force_skill)
            self._delegate_to_chatbot('error')
            return

        self.get_help(clean_skills, can_ignore_skill)

        self.state = self.STOP_STATE
        self.x_controller.break_execution()
        db.session.commit()

    @property
    def error_user_info(self):
        return u'id: {user.id}\nemail: {user.email}\ncampaign: {user.campaign.title}\nis_staff: {user.is_staff}'. \
            format(user=self.user)

    @chatbot_command_handler('post_to_slack')
    def post_to_slack(self, channel, message):
        try:
            slack.chat.post_message(channel, message, username='ChatBot', icon_emoji=':robot_face:')
        except Exception:  # pylint: disable=broad-except
            self.logger.exception('post_to_slack failed with error')

    @chatbot_command_handler('icr_card_for_journey')
    def _icr_card_for_journey(self, journey_id, campaign_type=''):
        if not isinstance(journey_id, int):
            journey_id = int(journey_id.replace(',', ''))
        child_journey = ChatbotChildJourney.load_by_id(journey_id, force=True)
        child_journey._icr_card(campaign_type=campaign_type)

    @chatbot_command_handler('dispo')
    def _dispo(self, name, details='', extras='', other_shit=''):
        tpa_name = self.user.tpa_name

        analytics.track(self.user, 'chatbot_dispo', {
            'journey': name,
            'event': details,
            'value': extras,
            'tpa_name': tpa_name
        })
        # analytic v2 - push event to different SegmentIO source
        analytics_v2.track(
            self.user.id,
            "Backend.Bot.Dispo",
            properties={
                "journey": name,
                "event": details,
                "value": extras,
                "tpa_name": tpa_name,
                "protocol_version": 2,
                # replace with semver once it's ready
                "build_version": os.getenv("BUILD_VERSION", "")
            }
        )
        if details in DISPO_SLACK_REPORT_EVENTS and current_app.config.get('SLACK_BOT_ERROR_CHANNEL'):
            message = u'*!@dispo {{{name}}} {{{details}}} {{{extras}}}*\n{user_info}'.format(
                name=name, details=details, extras=extras, user_info=self.error_user_info
            )
            try:
                slack.chat.post_message(current_app.config['SLACK_BOT_ERROR_CHANNEL'], message,
                                        icon_emoji=':robot_face:')
            except RequestException:
                pass
        self.send_to_chat(DispositionElement(name, details, extras))

    @chatbot_command_handler('set_header', translate_args=('title',))
    def set_header(self, title='', color='#FFFFFF', siska=1):
        self.set_env('journey_topic', title)
        try:
            siska = siska and bool(int(siska)) or False
        except (TypeError, ValueError):
            siska = True
        self.set_env('journey_siska', siska)
        self.say(HeaderParametersElement(title, color, siska))

    @chatbot_command_handler('request')
    def _response_to_chatbot(self, var):
        if '.' not in var:
            result = self._get_from_context(var)
        else:
            key = var.split('.', 1)[0]
            obj = self._get_from_context(key)
            try:
                result = ('{%s}' % var).format(**{key: obj})
            except AttributeError:
                result = None
        if result == '':
            result = None
        self._delegate_to_chatbot(smart_str(result))

    @chatbot_command_handler('simple_ticket', with_callback=True)
    def _simple_ticket(self, subject, body, priority=None, delay=None):
        user = self.user
        full_subject = u'BOT {bundle} {campaign} {subject}'.format(
            campaign=user.campaign.title,
            bundle=user.bundle.title,
            subject=subject
        )
        self.logger.warning(u'Simple ticket: command called: {}, {}, {}'.format(
            self.user.id,
            self.user.email,
            full_subject
        ))
        self._create_otrs_ticket(
            full_subject, message_smart_encode(body),
            bind_to_journey=False,
            priority=priority,
            delay=delay and int(delay.replace(',', ''))
        )

    @chatbot_command_handler('autocomplete_check', callback_on_error=True, report_error=False)
    def _autocomplete_check(self, source, filters=''):
        if not DataAutocompleteWrapper.check_entity_support(source):
            raise ValueError('Autocomplete {} is not supported'.format(source))

        filters_dict = self._tricky_parse_qsl(filters)
        items = data_api.autocomplete_wrap(source, term='', **filters_dict)
        if not items:
            raise ValueError('No autocomplete items for: {} {}'.format(source, filters))

        return self._delegate_to_chatbot(str(len(items)))

    def _move_to_autocomplete_state(self):
        if self.state != self.STATES.AUTOCOMPLETE:
            self.set_env('before_autocomplete_state', self.state)
            self.state = self.STATES.AUTOCOMPLETE

    @chatbot_command_handler(
        'autocomplete', callback_on_error=True, report_error=False, translate_args=('placeholder', 'header')
    )
    def _autocomplete_widget(self, source, filters='', required="1",
                             placeholder=None, check=False, header=None,
                             expand=False):
        if not DataAutocompleteWrapper.check_entity_support(source):
            raise ValueError('Autocomplete {} is not supported'.format(source))

        if check:
            self._autocomplete_check(source, filters=filters)
        self.make_and_send_x_control(AutocompleteElement, source=source,
                                     query_filter=urlencode(self._tricky_parse_qsl(filters)),
                                     required=required, placeholder=placeholder,
                                     header=header, expand=expand)
        self._move_to_autocomplete_state()

    @staticmethod
    def _split_choice_str(choices_str):
        return map(lambda x: x.strip().strip('"'), choices_str.split(','))

    @chatbot_command_handler(
        'select_widget', translate_args=(['choices_str', 'choices_translated'], 'default_text', 'header', 'text')
    )
    def _select_widget(self, text, choices_str, type_=None, default_text=None,
                       header=None, expand=False, default_first=None, choices_translated=None):
        choices_keys = self._split_choice_str(choices_str)
        choices_values = choices_translated and map(lambda x: x.strip().strip('"'), choices_translated.split(',')) \
            or choices_keys
        return self.send_x_control(make_select(
            self._state,
            default_text=default_text,
            default_first=default_first,
            choices=zip(choices_keys, choices_values),
            title=text.strip('"').strip(),
            header=header,
            expand=expand,
            type_=type_
        ))

    @chatbot_command_handler('simple_input', translate_args=('placeholder',))
    def _simple_input(self, title, placeholder=None, type_=None, pattern=None,
                      mask=None):
        self.make_and_send_x_control(
            InputElement,
            placeholder=placeholder and placeholder.strip('"').strip(),
            title=title,
            type_=type_,
            pattern=pattern,
            mask=mask,
        )

    @chatbot_command_handler('family_list', callback_on_error=True,
                             translate_args=('placeholder', 'default_text', 'header'))
    def _family_list(self, default_text=None, placeholder=None, header=None, expand=False, default_first=None,
                     show_add_family_member=False):
        profiles = [(self.user.profile.id, u'{} {}'.format(self.user.profile.full_name, gettext('(me)')))]
        if self.user.spouse:
            profiles.append((self.user.spouse.id, self.user.spouse.full_name))
        for relative in self.user.get_relatives_except(U2P_RELATION_SPOUSE, only_active=True). \
                order_by(UserProfile.birthday.asc().nullslast()):
            profiles.append((relative.id, relative.full_name))
        if show_add_family_member:
            profiles.append(('add_family_member', 'Add family member'))

        self.send_x_control(make_select(
            self._state,
            default_text=default_text and default_text.strip("'") or '',
            default_first=default_first,
            choices=profiles,
            title=placeholder,
            header=header,
            expand=expand,
            type_=SELECT_TYPE_STATIC_LIST
        ))
        self._move_to_autocomplete_state()

    @chatbot_command_handler('set_journey_data')
    def _set_journey_data(self, key, value):
        bot_data = self.env.get('bot_data', {})
        bot_data[key] = value
        self.set_env('bot_data', bot_data)
        self.set_env('bot_data_updated_at', datetime.now().strftime('%m-%d-%Y %H:%M:%S'))
        db.session.commit()

    @chatbot_command_handler('sleep', report_error=False)
    def _sleep(self, seconds):
        seconds = int(seconds.replace(',', ''))
        raise PostponeBotInstructions(timedelta(seconds=seconds), 'Wait!')

    @chatbot_command_handler('send_chat_link', translate_args=('title',))
    def _send_chat_link(self, url, type_, title, **kwargs):
        self.say(ChatURLElement(url, type_, title), **kwargs)

    @chatbot_command_handler('close_app')
    def _close_app(self):
        self.say(CloseAppElement())

    def start_child_journey_(self, cls, journey_article_content, journey_title=None, ticket_priority=None,
                             ticket_subject=None, dynamic_fields=None, aux_args=None, **extra_params):
        child = self.start_child(
            cls,
            activate=False,
            ticket_details=journey_article_content,
            ticket_priority=ticket_priority,
            ticket_subject=ticket_subject,
            journey_title=journey_title,
            dynamic_fields=dynamic_fields,
            aux_args=aux_args,
            **extra_params
        )
        db.session.commit()
        return child

    @chatbot_command_handler('get_journey_dynamic_reponse', callback_on_error=True)
    def get_journey_dynamic_reponse(self, journey_type):
        from ..models import JourneySettings
        try:
            cls = self.get_child_journey_class_by_type(journey_type)
        except ValueError:
            settings = None
        else:
            settings = JourneySettings.get_for_journey_controller(cls)
        self._delegate_to_chatbot(str(settings and settings.response_message_display or None))

    @chatbot_command_handler('get_chicago_dt', callback_on_error=True, report_error=False)
    def _get_chicago_dt(self, date_string, coords=None, tz=None, format_=None):
        format_ = format_ or '%B %d, %Y %I:%M %p'
        dt = dateparser_parse(date_string)
        if not tz:
            # TODO: GEO#3 flow google [REFACTORED]
            timestamp = time.mktime(dt.timetuple()) if dt else None
            lat, lon = coords.split(",")
            timezone_info = geo_service_client.get_timezone_info(lat, lon, timestamp=timestamp).get('timeZoneId')
            if not timezone_info:
                raise ValueError("Could not determine tz for coordinates {}".format(coords))
            tz = timezone_info['timeZoneId']
        dt = get_tz_datetime(dt, current_tz_name=tz, dest_tz_name='America/Chicago')
        self._delegate_to_chatbot(dt.strftime(format_))

    @chatbot_command_handler('check_permission')
    def _check_permission(self, permission):
        self.make_and_send_x_control(CheckPermissionElement, permission=permission)

    # ===================================== Find Care adapters =============================

    @chatbot_command_handler("set_provider_context")
    def _set_provider_context(self, npi):
        """Make `provider` object available in journey context"""
        if isinstance(npi, basestring):
            npi = int(npi.replace(",", ""))
        self.set_env("provider_npi", npi)

    def _get_provider_offices(self, npi, search_id=None, service_request_id=None):
        """Get relevant provider offices"""
        if isinstance(npi, basestring):
            npi = int(npi.replace(",", ""))

        search_id = search_id or None
        if isinstance(search_id, basestring):
            search_id = int(search_id.replace(",", ""))

        service_request_id = service_request_id or None
        if isinstance(service_request_id, basestring):
            service_request_id = int(service_request_id.replace(",", ""))

        facility_id = None
        if not search_id and not service_request_id:
            npis_facility_ids = med_profile_client.get_user_providers(self.user.id)
            facility_id = npis_facility_ids.get(npi)

        provider_data = provider_basket_client.get_provider_by_npi(
            npi, search_id=search_id, facility_id=facility_id
        )

        if service_request_id:
            service_request = service_requests_client.dc_get_service_request(service_request_id)
            decision = next((d["details"] for d in service_request["decisions"] if d["details"]["npi"] == npi), None)
            office_ids = []
            if decision:
                if service_request["type"] in ("provider", "generic_provider"):
                    office_ids = decision["office_ids"]
                elif service_request["type"] == "appointment":
                    office_ids = [decision["office_id"]]
            offices = [o for o in provider_data["offices"] if o["id"] in office_ids]
        else:
            provider_data = prepare_provider(provider_data, self.user)
            offices = provider_data["offices"]

        assert offices
        return offices

    @chatbot_command_handler("get_provider_single_office_id", callback_on_error=True, report_error=False)
    def _get_provider_single_office_id(self, npi, search_id, service_request_id):
        """Returns office id if there is a single relevant office, error otherwise"""
        offices = self._get_provider_offices(npi, search_id, service_request_id)
        assert len(offices) == 1
        self._delegate_to_chatbot(offices[0]["id"])

    @chatbot_command_handler(
        'send_provider_offices_select',
        callback_on_error=True,
        report_error=False,
        translate_args=('default_text', 'header', 'placeholder'),
    )
    def _send_provider_office_select(
            self, npi, search_id, service_request_id, default_text=None, placeholder=None, header=None, expand=False,
            default_first=None
    ):
        """Send multiselect with relevant provider offices as choices"""
        offices = self._get_provider_offices(npi, search_id, service_request_id)

        return self.send_x_control(make_select(
            self._state,
            default_text=default_text,
            default_first=default_first,
            choices=[(office["id"], office["address"]) for office in offices],
            title=placeholder or '',
            header=header,
            expand=expand,
            type_=SELECT_TYPE_STATIC_LIST
        ))

    @chatbot_command_handler("start_find_care_appointment_journey", with_callback=True)
    def _start_find_care_appointment_journey(
            self, npi, search_id, provider_service_request_id, requested_for_id, reason, office_id, appointment_prefs,
            appointment_prefs_other, selected_days, first_availability
    ):
        npi = int(npi.replace(",", ""))
        search_id = search_id and int(search_id.replace(",", "")) or None
        provider_service_request_id = (
            provider_service_request_id and int(provider_service_request_id.replace(",", "")) or None
        )
        office_id = int(office_id.replace(",", ""))
        requested_for_id = requested_for_id and int(requested_for_id.replace(",", "")) or None

        provider_data = provider_basket_client.get_provider_by_npi(npi, search_id=search_id)
        office = next(o for o in provider_data["offices"] if o["id"] == office_id)

        payload = {
            "type": "appointment",
            "legacy_user_id": self.user.id,
            "requested_for": requested_for_id,
            "source": "app",
            "status": "requested",
            "intake": {
                "npi": npi,
                "reason_for_visit": reason,
                "facility_id": office_id,
                "appointment_preferences": appointment_prefs,
                "other_appointment_preferences": appointment_prefs_other,
                "selected_days": selected_days,
                "first_availability": first_availability,
                "search_id": search_id,
                "parent_service_request_id": provider_service_request_id,
            },
        }
        service_request = service_requests_client.create_service_request(payload)

        profile = UserProfile.query.get(requested_for_id)
        content = "Card Name: Appointment\nPatient Name: {profile.full_name}\nReason: {reason}\n" \
                  "Provider Name: {provider_name}\nProvider Location: {address}\n" \
                  "Preference: {appointment_prefs}\n{appointment_prefs_other}\nSelected days: {selected_days}\n" \
                  "First Availability: {first_availability}".format(
                      profile=profile,
                      reason=reason,
                      provider_name=provider_data["name"],
                      address=office["address"],
                      appointment_prefs=appointment_prefs,
                      appointment_prefs_other=appointment_prefs_other,
                      selected_days=selected_days,
                      first_availability=first_availability,
                  )
        parent_service_request = (
            provider_service_request_id and
            service_requests_client.dc_get_service_request(provider_service_request_id)
        )
        parent_intake = parent_service_request and parent_service_request.get("intake")
        initial_location = parent_intake and parent_intake.get("initial_location") or profile.location_data

        return self.start_simple_child_journey(
            FindCareAppointmentJourney.journey_type,
            requested_for_id=requested_for_id,
            journey_article_content=content,
            extra_params=dict(service_request_id=service_request["id"], initial_location=initial_location, **payload),
        )

    # ===================================== Find Care adapters END =============================

    @chatbot_command_handler('start_simple_journey', with_callback=True)
    def start_simple_child_journey(
            self, journey_type, journey_article_content, journey_title=None, ticket_priority=None, ticket_subject=None,
            requested_for_id=None, dynamic_fields=None, aux_args=None, extra_params=None, **kwargs
    ):
        from ..models import JourneySettings
        cls = self.get_child_journey_class_by_type(journey_type)
        if self.user.campaign.is_demo_only and not cls.check_available_for_demo():
            return

        if extra_params is None:
            extra_params = {}
        extra_params.update(kwargs.pop('add_extra_params', True) and self.env.get(journey_type) or {})

        if requested_for_id and isinstance(requested_for_id, basestring):
            requested_for_id = int(requested_for_id.replace(',', ''))

        settings = JourneySettings.get_for_journey_controller(cls)
        if settings:
            ticket_priority = settings.ticket_priority or ticket_priority

        if dynamic_fields is None:
            dynamic_fields = {}

        initial_address = None
        if journey_type in (ProviderJourney.journey_type, NewFacilityJourney.journey_type,
                            AppointmentJourney.journey_type):
            initial_address, zip_code, state_city = self._get_address_details_for_journey()
            dynamic_fields.update({'zip1': zip_code, 'statecity1': state_city})

        elif journey_type in (FindCareProviderJourney.journey_type, FindCareAppointmentJourney.journey_type):
            address_components = self.env.get("intake", {}).get("initial_location")
            initial_address, zip_code, state_city = self._get_address_details_for_journey(address_components)
            dynamic_fields.update({'zip1': zip_code, 'statecity1': state_city})

        child_journey = self.start_child_journey_(
            cls, journey_article_content, journey_title=journey_title,
            ticket_priority=ticket_priority, ticket_subject=ticket_subject,
            dynamic_fields=dynamic_fields, aux_args=aux_args or [], initial_locale=self.user.supported_locale,
            requested_for_id=requested_for_id, initial_address=initial_address, **extra_params
        )
        if (
                isinstance(child_journey, ChatbotChildSimpleCardJourney) and
                not self.user.campaign.outbounds_enabled
        ):
            child_journey._icr_card()
        return child_journey

    @classmethod
    def get_child_journey_class_by_type(cls, journey_type):
        cls_ = {j.journey_type: j for j in _journeys.values() if getattr(j, 'journey_type', None)}.get(journey_type)
        if not cls_:
            raise ValueError('Unsupported journey type: {}'.format(journey_type))
        return cls_

    def _prepare_command_args(self, args, context=None, remove_spaces=False, convert_linebreaks=True,
                              translation_mode=False):
        from flask_babel import LazyString

        def translation_crutch(s, try_=False):
            s_to_translate = s.strip('"') if try_ else s
            if not s_to_translate:
                return s

            s_translated = self.translations.gettext(s_to_translate)
            if s_translated != s_to_translate:
                return s_translated

            if try_:
                return s

            return translation_crutch(s, try_=True)

        _args = []
        for a in args:
            if not isinstance(a, (basestring, LazyString)):
                _args.append(a)
                continue

            if translation_mode and not isinstance(a, LazyString):
                if a:
                    unicode_arg = smart_str(a).decode('utf-8')
                    simple_logger.info(u'[TR] processing bot arg "%s", loc: %s, (%s)'
                                       % (unicode_arg, get_locale(), self.user.id))
                    a = translation_crutch(unicode_arg)

            if context is None:
                _args.append(a)
                continue

            try:
                a = format_chatbot_message(a, context=context)
            except TemplateError:
                pass

            if remove_spaces:
                a = a.replace(' ', '')
            if convert_linebreaks:
                a = a.replace(CHATBOT_LINEBREAK_TOKEN, '\n')
            _args.append(a)
        return _args

    def _prepare_command_vars(self, func, extra_context, *args, **kwargs):
        translation_mode = kwargs.pop('translation_mode', False)
        _args = self._prepare_command_args(args, context=extra_context, remove_spaces=func._remove_spaces,
                                           convert_linebreaks=func._convert_linebreaks,
                                           translation_mode=translation_mode)
        available_vars = func.__code__.co_varnames[len(_args) + 1:]
        return _args, dict((k, v) for k, v in kwargs.iteritems() if k in available_vars)

    def _have_chatbot_command(self, command):
        return command in self.chatbot_command_registry

    def _handle_chatbot_command(self, command, *orig_args, **orig_kwargs):
        if not self._have_chatbot_command(command):
            self._chatbot_fallback()
            self.logger.error('ChatBot command not found! Thread: %s, command: %s', self.thread.id, command)
            return

        func_name = self.chatbot_command_registry[command]
        func = getattr(self, func_name)
        extra_context = None
        passed_context = orig_kwargs.pop('extra_context', None) or {}
        if func._format_args:
            extra_context = self._chatbot_context
            extra_context.update(passed_context)
            extra_context.update(self.env.get('extra_context', {}))

        args, kwargs = self._prepare_command_vars(func, extra_context, *orig_args, **orig_kwargs)
        translate_args = func._translate_args
        if translate_args and self.user.locale:
            tr_args, tr_kwargs = self._prepare_command_vars(func, extra_context, *orig_args,
                                                            translation_mode=True, **orig_kwargs)
            if translate_args is translate_all:
                args, kwargs = tr_args, tr_kwargs
            else:
                func_varnames = func.__code__.co_varnames[1:]
                for arg_to_translate in translate_args:
                    dest_kwarg_name = None
                    if isinstance(arg_to_translate, (list, tuple)):
                        arg_name, dest_kwarg_name = arg_to_translate
                    else:
                        arg_name = arg_to_translate

                    arg_index = func_varnames.index(arg_name)
                    if arg_index < len(tr_args):
                        source = tr_args
                        key = arg_index
                        dest = args
                    else:
                        source = tr_kwargs
                        key = arg_name
                        dest = kwargs

                    try:
                        val = source[key]
                    except KeyError:
                        continue

                    if dest_kwarg_name is None:
                        dest[key] = val
                    else:
                        kwargs[dest_kwarg_name] = val

        # TODO: complete this shit

        from healthjoy.assistant.backend.jabber_tool_client import JabberToolServiceUnavailable

        try:
            res = func(*args, **kwargs)
        except JabberToolServiceUnavailable as e:
            raise e
        except Exception as e:
            if func._report_error or not isinstance(e, (AssertionError, ValidationError, PostponeBotInstructions)):
                self.logger.exception(
                    'ChatBot command failed! Thread: %s, command: %s, user_id: %s',
                    self.thread.id,
                    command,
                    self.user.id,
                )
                if func._report_error:
                    if current_app.config.get('SLACK_BOT_ERROR_CHANNEL'):
                        error_message = u'Chatbot command {}({}) failed with: {}'.format(command, ', '.join(args), e)
                        try:
                            slack.chat.post_message(current_app.config['SLACK_BOT_ERROR_CHANNEL'], u'*{}*\n{}'.
                                                    format(error_message, self.error_user_info),
                                                    icon_emoji=':robot_face:')
                        except RequestException:
                            pass

            if func._with_callback or func._callback_on_error:
                self.x_controller.controls = 0
                self._delegate_to_chatbot('error')
        else:
            if func._with_callback:
                self._delegate_to_chatbot('success')
            return res


class ChatbotJourney(ChatbotBaseJourney, DefaultJourney):
    STATES = enum(
        'ChatbotJourneyStates',
        NEW='new',
        CHATBOT_CONVERSATION='chatbot_conversation',
        WATSON_CONVERSATION='watson_conversation',
        GREETING_SENT='greeting_sent',
        CHATBOT_FINISHED='chatbot_finished',
        AUTOCOMPLETE='autocomplete',
        PHOTO_UPLOAD='photo_upload',
        FINISHED='finished',
        QUESTION='question',
        LOCATION_INFO='location_info',
        FEEDBACK='feedback',
    )
    INIT_STATE = STATES.NEW
    DEFAULT_STATE = STATES.CHATBOT_CONVERSATION
    STOP_STATE = STATES.FINISHED

    def handle_wake_up_default(self):
        if self.state in [self.STATES.FINISHED, self.STATES.NEW]:
            return self.STATES.CHATBOT_CONVERSATION

    def proceed_to_tiles(self, title=None, tiles_extra=None, tiles_id=None, notify_bot=False):
        if notify_bot:
            self._send_to_bot('[new_chat]', safe=False)
        self.update_thread_state_as_bot()
        return super(ChatbotBaseJourney, self).proceed_to_tiles(title=title, tiles_extra=tiles_extra, tiles_id=tiles_id)

    @chatbot_command_handler('start_simple_journey_context', with_callback=True)
    def start_simple_child_journey_context(self, journey_type, context_key, journey_title=None,
                                           ticket_priority=None, ticket_subject=None, dynamic_fields=None, *args):
        cls = self.get_child_journey_class_by_type(journey_type)
        journey_article_content = "\n---------------\n".join(self._state.parameters.get(context_key, []))

        self.start_child_journey_(cls, journey_article_content, journey_title=journey_title,
                                  ticket_priority=ticket_priority, ticket_subject=ticket_subject,
                                  dynamic_fields=dynamic_fields, aux_args=args)

    def update_thread(self, thread, silent=False, update_state=False):
        should_say = not silent and (self.thread_id != thread.id or thread.thread_status == ThreadStatus.Initial)
        self._was_thread_bot_tagged = thread and thread.thread_status == ThreadStatus.WithBot
        super(ChatbotJourney, self).update_thread(thread=thread)
        if update_state and self.state == self.STOP_STATE:
            self.state = self.STATES.CHATBOT_CONVERSATION
        if should_say:
            self.not_first_thread_start()

    def first_thread_start(self):
        user = self.user
        if user.onboarded:
            self.logger.warning('Chatbot user is onboarded')
            self.not_first_thread_start()
            return

        self.logger.warning('Chatbot first thread start')
        self.state = self.STATES.CHATBOT_CONVERSATION
        self._delegate_to_chatbot('[onboarding]', fallback=self.send_tiles, safe=False)

    def not_first_thread_start(self):
        user = self.user
        if not user.onboarded:
            self.logger.warning('Chatbot user is not onboarded')
            self.first_thread_start()
            return

        self.state = self.STATES.CHATBOT_CONVERSATION
        self.logger.warning('Chatbot non first thread start')
        self._delegate_to_chatbot('[new chat]', fallback=self.send_tiles, safe=False)

    @classmethod
    def ensure_thread_journey(cls, thread):
        try:
            journey = cls.load_by_chat_profile_id(thread.profile_id)
        except JourneyNotFound:
            journey = cls.create(thread, start=False)
        else:
            journey.update_thread(thread, silent=True, update_state=True)
            journey._state.archived = False
        return journey

    @classmethod
    def ensure_active_journey(cls, thread):
        journey = cls.ensure_thread_journey(thread)
        if not journey.is_active:
            journey.activate()
            db.session.commit()
        return journey

    @classmethod
    def force_message(cls, thread, message, fallback=None, to_chat=False):
        j = cls.ensure_active_journey(thread)
        j.state = j.STATES.CHATBOT_CONVERSATION
        if to_chat:
            j.send_to_chat(message)
        else:
            j._delegate_to_chatbot(message, safe=False, fallback=fallback)
        return j

    @classmethod
    def force_message_to_active_thread(cls, user, message, fallback=None, to_chat=False):
        thread = user.open_chat or create_outbound_chat(user, blank=True)
        return cls.force_message(thread, message, fallback=fallback, to_chat=to_chat)

    @chatbot_command_handler('check_choice', with_callback=True)
    def _check_choice(self, choices_str, choice):
        choices = self._split_choice_str(choices_str)
        assert choice in choices

    @chatbot_command_handler(
        'multiselect', translate_args=(['choices_str', 'choices_translated'], 'default_text', 'header',)
    )
    def _send_multiselect(self, title, choices_str, default_text=None, header=None, expand='1',
                          default_first=None, max_choices=0, choices_translated=None):
        if not default_text:
            default_text = gettext('Nothing from above')

        choices_keys = self._split_choice_str(choices_str)
        choices_values = choices_translated and self._split_choice_str(choices_translated) \
            or choices_keys

        return self.send_x_control(make_select(
            self._state,
            default_text=default_text,
            default_first=default_first,
            choices=zip(choices_keys, choices_values),
            title=title.strip('"').strip(),
            header=header,
            expand=expand,
            type_=SELECT_TYPE_MULTISELECT,
            cls=MultipleSelectElement,
            max_choices=max_choices,
        ))

    @chatbot_command_handler('format_datetime', callback_on_error=True)
    def _format_datetime(self, datetime_str):
        dt = dateutil_parse(datetime_str)
        self._delegate_to_chatbot(dt.strftime('%A, %B %d, %I:%M %p'))

    @chatbot_command_handler('mbr_free_days', translate_args=('default_text', 'header', 'placeholder'))
    def _mbr_free_days(self, expand='1', default_text=None, header=None, title=None, placeholder=None, tz=None):
        if default_text is None:
            default_text = gettext('Nothing from above')

        mbr_slots = SlotsManager(user_timezone=tz)
        empty_slots_dates = mbr_slots.get_empty_slots_dates()[:7]
        choices = [(d.isoformat(), d.strftime('%a %d %b').replace(' 0', ' ')) for d in empty_slots_dates]

        self.send_x_control(make_select(
            self._state,
            default_text=default_text,
            default_first=True,
            choices=choices,
            title=placeholder or '',
            header=header,
            expand=expand,
            type_=SELECT_TYPE_HORIZONTAL
        ))
        # crutch: we should return to chatbot response as on autocomplete
        self._move_to_autocomplete_state()

    @chatbot_command_handler(
        'mbr_free_day_times',
        callback_on_error=True,
        translate_args=('default_text', 'header', 'placeholder'),
        report_error=False,
    )
    def _mbr_free_day_times(self, date_string, expand='1', default_text=None, header=None, title=None,
                            placeholder=None, tz=None):
        if default_text is None:
            default_text = gettext('Select another date')

        date_ = dateparser_parse(date_string)
        choices = list()
        for d in SlotsManager(user_timezone=tz).get_empty_slots(date_):
            choice = d.strftime('%I:%M %p')
            choices.append(choice)
        db.session.commit()
        assert choices

        self.send_x_control(make_select(
            self._state,
            default_text=default_text,
            default_first=True,
            choices=choices,
            title=title or '',
            header=header,
            expand=expand,
            type_=SELECT_TYPE_HORIZONTAL,
        ))

    @chatbot_command_handler('fill_mbr_slot', with_callback=True)
    def _fill_mbr_slot(self, date_str, time_str, tz, phone, content):
        """
        Deprecated: should be removed after bot functional will move on '_fill_slot_for_existing_mbr'.
        """
        obj = self._extra_context_obj
        dt_isodate = obj.get('id')
        date_ = dt_isodate and dateparser_parse(dt_isodate) or dateparser_parse(date_str)
        formatted_date = date_.strftime('%A %B %d, %Y')
        args = [formatted_date, ' '.join([time_str, tz]), phone]
        time_ = datetime.strptime(time_str, '%I:%M %p').time()

        combine_datetime = datetime.combine(date_, time_)
        combine_datetime = get_tz_datetime(combine_datetime, current_tz_name=tz or 'America/Chicago',
                                           dest_tz_name='UTC')

        slot = MedicalBillReviewAppointmentSlot.schedule(combine_datetime)
        child_journey = self.start_simple_child_journey(
            journey_type='medical_bill',
            journey_article_content=content,
            journey_title='Bill Review',
            aux_args=args,
        )
        slot.child_journey_state_id = child_journey.id
        db.session.commit()

    @chatbot_command_handler('chat_instance', translate_args=translate_all)
    def _send_presentation(self, title, description=None):
        self.say(SimpleInstanceElement(title, description=description))

    @chatbot_command_handler('chat_hint', translate_args=translate_all)
    def _chat_hint_widget(self, *args):
        self.say(HintElement(args))

    @chatbot_command_handler('send_tiles')
    def _send_tiles(self, show_menu_delay=None, title=''):
        self.send_tiles(title and title.strip('"'), tiles_extra=dict(show_menu_delay=show_menu_delay))
        db.session.commit()

    def send_tiles(self, title=None, tiles_extra=None, tiles_id=None):
        self.logger.debug(
            "%s %s adding control from send_tiles", self, self.x_controller.uid
        )
        self.x_controller.add_control()
        return super(ChatbotJourney, self).send_tiles(title=title, tiles_extra=tiles_extra, tiles_id=tiles_id)

    @chatbot_command_handler('zipcode_input_widget', translate_args=('placeholder',))
    def _zipcode_input_widget(self, text='', placeholder=None):
        self.make_and_send_x_control(ZipcodeInputElement,
                                     title=text.strip('"').strip(),
                                     placeholder=placeholder)

    @chatbot_command_handler('workday_input_widget', translate_args=('placeholder',))
    def _workday_picker_widget(self, text='', placeholder=None):
        self.make_and_send_x_control(WorkdayInputElement,
                                     title=text.strip('"').strip(),
                                     placeholder=placeholder)

    @chatbot_command_handler('phone_input_widget', translate_args=('placeholder',))
    def _phone_input_widget(self, text='', placeholder=None):
        self.make_and_send_x_control(PhoneInputElement,
                                     title=text.strip('"').strip(),
                                     placeholder=placeholder)

    @chatbot_command_handler('date_input_widget', translate_args=('placeholder',))
    def _dob_input_widget(self, text='', placeholder=None, default=None, restrict_to=None):
        self.make_and_send_x_control(DateInputElement,
                                     title=text.strip('"').strip(),
                                     placeholder=placeholder,
                                     default=default,
                                     restrict_to=restrict_to)

    @chatbot_command_handler(
        'predefined_list',
        callback_on_error=True,
        report_error=False,
        translate_args=('placeholder', 'header', 'default_text'),
    )
    def _predefined_list(self, alias, default_text=None, placeholder=None,
                         header=None, expand=False, default_first=None):
        choices = static_list_registry.get(alias)
        self.send_x_control(make_select(
            self._state,
            default_text=default_text,
            default_first=default_first,
            choices=[(k, unicode(v)) for k, v in choices],
            title=placeholder or '',
            header=header,
            expand=expand,
            type_=SELECT_TYPE_STATIC_LIST
        ))

    @chatbot_command_handler(
        'search_list',
        callback_on_error=True,
        report_error=False,
        translate_args=('placeholder', 'header', 'default_text'),
    )
    def _search_list(self, entity, filters='', default_text=None,
                     placeholder=None, header=None, expand=False, default_first=None):
        search_func = data_api.search_wrap
        filters = self._tricky_parse_qsl(filters)
        if not DataItemsWrapper.check_entity_support(entity):
            if DataAutocompleteWrapper.check_entity_support(entity):
                search_func = data_api.autocomplete_wrap
                filters['limit'] = filters.get('limit', 100)
            else:
                raise ValueError(
                    'Search for {} is not supported'.format(entity))

        items = search_func(entity, filters.pop('query', ''), **filters)
        if not len(items):
            raise ValueError(
                'No items found for: {} with filters {}'.format(entity,
                                                                filters))

        self.send_x_control(make_select(
            self._state,
            default_text=default_text,
            default_first=default_first,
            choices=[(i['id'], i['name']) for i in items],
            title=placeholder,
            header=header,
            expand=expand,
            type_=SELECT_TYPE_STATIC_LIST
        ))
        self._move_to_autocomplete_state()

    @chatbot_command_handler('photo')
    def _photo(self, force=None):
        self.make_and_send_x_control(PhotoElement, force=force)
        self.state = self.STATES.PHOTO_UPLOAD

    @chatbot_command_handler('file_upload')
    def _file_upload(self, force=None):
        self.make_and_send_x_control(FileUploadElement, force=force)
        self.state = self.STATES.PHOTO_UPLOAD

    @chatbot_command_handler('set_plan_by_id')
    def _set_plan_by_id(self, plan_id, variance=1, verify=True):
        plan = self.core_benefits.set_plan_by_id(plan_id, primary=True, variance=variance)
        if verify:
            plan.verify()
        db.session.commit()

    @chatbot_command_handler('verify_plan')
    def _verify_plan(self, type_=MEDICAL_PLAN_TYPE):
        self.core_benefits.verify_plan(True, type_=type_)
        db.session.commit()

    @chatbot_command_handler('unverify_plan')
    def _unverify_plan(self, type_=MEDICAL_PLAN_TYPE):
        self.core_benefits.verify_plan(False, type_=type_)
        db.session.commit()

    @chatbot_command_handler('is_verified')
    def _is_verified(self, type_=MEDICAL_PLAN_TYPE):
        plan = self.core_benefits.get_any_primary_plan_for_type(type_)
        self._delegate_to_chatbot(plan and plan.verified and 'True' or 'False')

    @chatbot_command_handler('remove_plans')
    def _remove_plans(self):
        self.core_benefits.delete()
        db.session.commit()

    @chatbot_command_handler('get_doctor_details', callback_on_error=True, report_error=False)
    def _get_doctor_details(self, doctor_npi):
        doctor_npi = doctor_npi.replace(',', '')
        doctor = data_api.retrieve_doctor(doctor_npi)
        self._delegate_to_chatbot(json.dumps(doctor))

    @chatbot_command_handler('get_single_drug_details', callback_on_error=True, report_error=False)
    def _get_single_drug_details(self, group_hash):
        drug = data_api.get_single_drug(group_hash=group_hash)
        self._delegate_to_chatbot(json.dumps(drug))

    @chatbot_command_handler('mail_order_prog', callback_on_error=True)
    def _mail_order_prog(self, group_hash):
        mops = [p.name for p in self.user.campaign.mail_order_programs]
        if not mops:
            self._delegate_to_chatbot(str(None))
            return

        is_hsa = bool(self.user.plan and self.user.plan.hsa_eligible)

        # tmp crutch for Khem
        default_mop = HealthJoyConfigs.get_value('DEFAULT_MAIL_ORDER_PROGRAM', "RX'nGo")
        mops.sort(key=lambda x: x != default_mop)
        # ===================

        mops_gen = (
            mop for mop in mops if
            data_api.drug_in_mail_order_program(group_hash=group_hash, program=mop, is_hsa=is_hsa)
        )
        self._delegate_to_chatbot(str(next(mops_gen, None)))

    @chatbot_command_handler('update_profile_data', with_callback=True)
    def _update_profile_data(self, update_args):
        profile = self.user.profile
        for attr, val in self._tricky_parse_qsl(update_args).items():
            if attr == 'gender' and val not in GENDERS:
                continue
            setattr(profile, attr, val)
        db.session.commit()

    @chatbot_command_handler('add_family_profile', callback_on_error=True)
    def _add_family_profile(self, first_name, last_name, relation_type, dob, gender, covered=True):

        dob_ = dob and dateutil_parse(dob)
        if dob_ and relation_type == U2P_RELATION_DEPENDENT and is_adult(dob_):
            relation_type = U2P_RELATION_ADULT_CHILD

        from healthjoy.auth.forms import RelativeForm
        form = RelativeForm(request_data_to_multidict(dict(
            name=first_name,
            second_name=last_name,
            relation=relation_type,
            birthday=dob and dateutil_parse(dob).date().isoformat(),
            gender=gender and gender.lower()
        )), csrf_enabled=False)
        if not form.validate():
            if not form.is_dependent_age_valid:
                self._delegate_to_chatbot('dependent_age_error')
                return

            errors = form.errors
            raise ValidationError('Invalid data for relation', errors=errors)

        relative = UserProfile(
            user_id=self.user.id,
            relation=form.relation.data,
            covered=bool(covered),
            source=UserProfileSources.BOT_JOURNEY,
        )
        form.populate_obj(relative)
        if not relative.zip:
            relative.copy_address(self.user.profile)
        db.session.add(relative)
        db.session.commit()
        self._delegate_to_chatbot(relative.id)

    @chatbot_command_handler('onboarded')
    def _onboarded(self):
        self.user.onboarded = True
        db.session.commit()

    @chatbot_command_handler('telemed_consult')
    def _telemed_consult(self):
        self.say(ConversationBrowseElement(get_telemed_url(self.user)))

    @chatbot_command_handler('add_hsa_account')
    def _add_hsa_account(self, enter_point='finance'):
        self.set_env('enter_point', enter_point)
        self.say(ConversationBrowseElement('/views/plaid'))

    @chatbot_command_handler("send_test_push")
    def _send_test_pust(self, link='/views/benefits', text="test_message"):
        send_mobile_push.delay(
            self.user.id,
            text,
            category=APS_LINK_CATEGORY,
            link=link,
            title='test push',
            eta=timedelta(seconds=30)
        )

    def ask_question(self, question_info):
        self.set_env('question_info', question_info)
        if self.state == self.STATES.QUESTION:
            return

        self.set_header('Chat')
        msg = gettext('Ok, what is your question?')
        self.say(msg)
        self._simple_input(msg)
        self.state = self.STATES.QUESTION

    def handle_submit_when_question(self, el):
        self.say(el, msg_id=el.id)
        question_info = self.env.get('question_info')
        if question_info:
            self._whisper(question_info)
        self.get_help()
        self.state = self.STOP_STATE
        self.x_controller.break_execution()

    def _get_group_plan_ids(self, plan_kind=None):
        plan_section = self.user.group_plan_ids_by_kind
        if plan_kind:
            plans_ids = plan_section.get(plan_kind) or []
        else:
            plans_ids = list(itertools.chain(plan_section.values()))
        return plans_ids

    @chatbot_command_handler('has_group_plan', callback_on_error=True)
    def _has_group_plan(self, plan_kind=None):
        self._delegate_to_chatbot(self._get_group_plan_ids(plan_kind) and 'True' or 'False')

    @chatbot_command_handler('get_group_single_plan', callback_on_error=True, report_error=False)
    def _get_group_single_plan(self, plan_kind=None):
        plans_ids = self._get_group_plan_ids(plan_kind)
        assert len(plans_ids) == 1
        plan = data_api.retrieve_plan(plans_ids[0])
        plan_name = plan['name']
        plan_data = dict(id=plan['plan_id'], name=plan_name)
        self.save_context_obj(plan_data)
        self._delegate_to_chatbot(plan_name)

    @chatbot_command_handler(
        'group_plan_list', callback_on_error=True, translate_args=('placeholder', 'header', 'default_text')
    )
    def _group_plan_list(self, plan_kind=None, placeholder=None, default_text=None, header=None, expand=False,
                         default_first=None):
        assert self.user.campaign.is_groups
        plans_ids = self._get_group_plan_ids(plan_kind)
        assert plans_ids
        plans = data_api.plan_batch(plans_ids)
        self.send_x_control(make_select(
            self._state,
            default_text=default_text,
            default_first=default_first,
            choices=[(plan['plan_id'], plan['name']) for plan in plans],
            title=placeholder or '',
            header=header,
            expand=expand,
            type_=SELECT_TYPE_STATIC_LIST
        ))
        # crutch: we should return to chatbot response as on autocomplete
        self._move_to_autocomplete_state()

    @chatbot_command_handler('search_count', callback_on_error=True, report_error=False)
    def _search_count(self, entity, filters=''):
        filters = self._tricky_parse_qsl(filters)
        wrap_func = data_api.count_wrap
        if not DataCountWrapper.check_entity_support(entity):
            if DataItemsWrapper.check_entity_support(entity):
                wrap_func = data_api.search_wrap
            elif DataAutocompleteWrapper.check_entity_support(entity):
                wrap_func = data_api.autocomplete_wrap
            else:
                raise ValueError('Count for {} is not supported'.format(entity))

        wrapper = wrap_func(entity, filters.pop('query', ''), **filters)
        self._delegate_to_chatbot(str(wrapper.count))

    @chatbot_command_handler('set_no_insurance')
    def _set_no_insurance(self, type_=MEDICAL_PLAN_TYPE, verify=True):
        self.core_benefits.get_or_create_no_core_plan(type_)
        if verify:
            self.core_benefits.verify_plan(type_=type_)
        db.session.commit()

    @chatbot_command_handler('drop_insurance_plan')
    def _drop_insurance_plan(self, type_=MEDICAL_PLAN_TYPE):
        plan = self.core_benefits.get_primary_plan_for_type(type_)
        if plan:
            db.session.delete(plan)
            db.session.commit()

    @chatbot_command_handler('drug_forms_check', with_callback=True, report_error=False)
    def _drug_forms_check(self, drug_id, is_brand=None):
        extra_kwargs = {}
        if is_brand:
            extra_kwargs.update(is_brand=is_brand)
        assert data_api.get_drug_dosage_forms(drug_id, **extra_kwargs), \
            'Not found dosage forms for medications: {}'.format(drug_id)

    @chatbot_command_handler('drug_strength_check', with_callback=True, report_error=False)
    def _drug_strength_check(self, drug_id, dosage_form, is_brand=None):
        extra_kwargs = {}
        if is_brand:
            extra_kwargs.update(is_brand=is_brand)
        strength_list = data_api.get_medication_strength(drug_id, dosage_form, **extra_kwargs) or []
        strength_choices = [(s['display_strength'], s['display_strength'])
                            for s in strength_list if s.get('display_strength')]
        assert strength_choices, 'Not found strength for medications: {}'.format(drug_id)

    @chatbot_command_handler(
        'drug_forms_list', callback_on_error=True, translate_args=('placeholder', 'header', 'default_text')
    )
    def _drug_forms_list(self, drug_id, default_text=None, placeholder=None, header=None,
                         expand=False, is_brand=None, default_first=None):
        extra_kwargs = {}
        if is_brand:
            extra_kwargs.update(is_brand=is_brand)
        dosages = data_api.get_drug_dosage_forms(drug_id, **extra_kwargs)
        assert dosages
        self.send_x_control(make_select(
            self._state,
            default_text=default_text,
            default_first=default_first,
            choices=[(i, i) for i in dosages],
            title=placeholder or '',
            header=header,
            expand=expand,
            type_=SELECT_TYPE_STATIC_LIST
        ))

    @chatbot_command_handler(
        'drug_strength_list', callback_on_error=True, translate_args=('placeholder', 'header', 'default_text')
    )
    def _drug_strength_list(self, drug_id, dosage_form, default_text=None, placeholder=None,
                            header=None, expand=False, is_brand=None, default_first=None):
        extra_kwargs = {}
        if is_brand:
            extra_kwargs.update(is_brand=is_brand)
        strength = data_api.get_medication_strength(drug_id, dosage_form, **extra_kwargs)
        strength_choices = [(s['display_strength'], s['display_strength'])
                            for s in strength if s.get('display_strength')]
        assert strength_choices
        self.send_x_control(make_select(
            self._state,
            default_text=default_text,
            default_first=default_first,
            choices=strength_choices,
            title=placeholder or '',
            header=header,
            expand=expand,
            type_=SELECT_TYPE_STATIC_LIST
        ))

    @chatbot_command_handler('start_rx')
    def _start_rx_state(self):
        self.set_env(RxSavingsJourney.journey_type, {'rx_medications': []})

    @chatbot_command_handler('store_profile_medication')
    def _store_profile_medication(self, profile_id, name, rx_form, rx_dose, frequency):
        from healthjoy.assessment.jobs import sync_record

        current_medications = self.env.get(RxSavingsJourney.journey_type, {}).get('rx_medications') or []
        self.set_env(RxSavingsJourney.journey_type, {
            'rx_medications': current_medications + [dict(name=name, dose=rx_dose, form=rx_form, frequency=frequency)]
        })
        profile_id = int(profile_id.replace(',', ''))
        medication = UserProfileMedication(profile_id=profile_id, name=name, dosage_form=rx_form, strength=rx_dose,
                                           frequency=frequency, active=True, source=ASSESSMENT_SOURCE_RX)
        sync_record(medication)
        self._delegate_to_chatbot('medication_added')

    @chatbot_command_handler('get_address_details', callback_on_error=True)
    def _get_address_details(self, address):
        address_components = geo_service_client.geocode_address(address)
        self.save_context_obj({
            'map_url': address_components.get('map_static_url'),
            'zip': address_components.get('zip_code'),
        })
        self._delegate_to_chatbot(address_components.get('zip_code'))

    @chatbot_command_handler('check', callback_on_error=True)
    def check_value(self, data_type, val, only_validate_existing=False):
        validate = VALIDATORS[data_type]
        user_id = None if only_validate_existing else self.user.id
        try:
            validate(user_id, val, only_validate_existing)
        except UserAlreadyExistsValidationerror:
            result = 'already_associated'
        else:
            result = 'success'
        self._delegate_to_chatbot(result)

    @chatbot_command_handler('start_qna', with_callback=True, report_error=False)
    def _start_qna(self, question):
        self.start_child(ArticleJourney, activate=False, question=question)
        db.session.commit()

    @chatbot_command_handler('start_outbound_journey')
    def start_intake_journey(self, fire_command=None, messages=None, campaign_type='', use_strict_delivery=False,
                             close_immediately=False, archive=False, **kwargs):
        from ..jobs import add_journey_to_delivery
        j = self.start_child(IntakeJourney, activate=False, fire_command=fire_command, messages=messages,
                             campaign_type=campaign_type, extra_kwargs=kwargs)
        if archive:
            j.archive()
        db.session.commit()
        add_journey_to_delivery.delay(
            j.id,
            j.STOP_STATE,
            use_strict_delivery=use_strict_delivery,
            clear_intake=False,
            close_immediately=close_immediately,
        )
        return j

    def start_spitball_journey(self, fire_command, campaign_type='', **kwargs):
        from healthjoy.icr.jobs import clear_intake_from_delivery
        clear_intake_from_delivery(self.user.id)
        return self.start_intake_journey(fire_command, campaign_type=campaign_type, **kwargs)

    @chatbot_command_handler('note')
    def _note(self, key, val):
        self.user.set_note(key, val)
        db.session.commit()

    @chatbot_command_handler('set_value', report_error=False)
    def _set_value(self, key, val):
        set_chatbot_value(self.user, key, val)
        db.session.commit()

    @chatbot_command_handler('completed_journey', with_callback=True)
    def start_diy_journey(self, journey_type, card_question='', card_response='', title=None):
        j_cls = self.get_child_journey_class_by_type(journey_type)
        if not getattr(j_cls, 'has_diy'):
            raise ValueError('DIY for {} is not supported'.format(journey_type))

        card_response = '<br/><br/>'.join([card_question or '', card_response or ''])
        child_journey = self.start_child(
            j_cls,
            card_response=card_response,
            activate=False,
            title=title
        )
        db.session.commit()
        child_journey._send_journey_presentation()

    @chatbot_command_handler('get_age')
    def _get_age(self, profile_id=None):
        if isinstance(profile_id, basestring):
            profile_id = profile_id.replace(',', '')

        if not profile_id.isdigit():
            self._delegate_to_chatbot('error')
            return

        profile = UserProfile.query.get(profile_id) if profile_id else self.user.profile
        self._delegate_to_chatbot(str(profile and profile.age or None))

    @chatbot_command_handler('check_zip', callback_on_error=True)
    def _check_zip(self, zip_code):
        """
        Check if zip code is valid
        :param zip_code: zip code to check
        :return: 'True' if valid, else 'False' (as string)
        """
        zip_code = zip_code.replace(',', '')
        # TODO: flow GEO#2 [REFACTORED]
        try:
            is_valid = bool(zip_code and geo_service_client.get_zip_info(zip_code=zip_code))
        except DataError:
            is_valid = False
            db.session.rollback()

        self._delegate_to_chatbot(str(is_valid))

    @chatbot_command_handler('location_info_by_zip', callback_on_error=True)
    def _location_info_by_zip(self, zip_code):
        """
        Gets user location details by zip_code.
        :param zip_code: zip code
        :return: None
        """
        zip_code = zip_code.replace(',', '')
        zip_info = geo_service_client.get_zip_info(zip_code=zip_code)
        if zip_info:
            zip_info['formatted_address'] = "{}, {} {}".format(
                zip_info['city'], zip_info['state'], zip_info['zip_code'],
            )
            self.save_context_obj(zip_info, 'address_components')

    @chatbot_command_handler('reset_location_info', callback_on_error=True)
    def _reset_location_info(self):
        """
        Remove 'address_components' from self.env
        :return: None
        """
        self.del_from_env('address_components')

    @chatbot_command_handler('whisper')
    def _whisper(self, msg):
        self.say(msg, user_invisible=True, is_whisper=True)

    @chatbot_command_handler('request_permission')
    def _request_permission(self, permission):
        self.make_and_send_x_control(RequestPermissionElement, permission=permission)

    @chatbot_command_handler('permission_settings')
    def _permission_settings(self, permission):
        self.say(PermissionSettingsElement(permission=permission.lower()))

    @chatbot_command_handler('get_location', format_args=False)
    def _get_location(self, format_=''):
        self.make_and_send_x_control(GetLocationElement, format_=format_)
        self.set_env('before_location_info_state', self.state)
        self.state = self.STATES.LOCATION_INFO

    # ===================================== EAP adapters =============================

    def _get_custom_eap_card(self):
        """Return user wallet cart if categiry contains EAP."""
        auth_token = create_access_token(self.user.ext_id)
        company_data = cmanager_client.get_company_info(self.user.campaign.alias)
        plan_periods = company_data['plan_periods']
        plan_period_id = plan_periods[0]['id']

        cards = get_user_wallet_cards(
            self.user, plan_period_id=plan_period_id, min_date=None, max_date=None,
            auth_token=auth_token
        )
        return next((card for card in cards if card["category"] == "eap"), None)

    @chatbot_command_handler('set_eap_wallet_card_link', report_error=False, with_callback=True)
    def _set_eap_wallet_card_link(self):
        wallet_card_url = None
        if self.user.is_eap_available:
            wallet_card_url = "/v2/wallet/hj_eap"
        else:
            card = self._get_custom_eap_card()
            if card:
                wallet_card_url = "/v2/wallet/eap"

        assert wallet_card_url

        self.set_env('user_eap_wallet_card_url', wallet_card_url)
        db.session.commit()

    @chatbot_command_handler('send_eap_wallet_card_link')
    def _send_eap_wallet_card_link(self):
        wallet_card_url = self.env.get('user_eap_wallet_card_url')
        self.del_from_env('user_eap_wallet_card_url')
        db.session.commit()

        self._send_chat_link(wallet_card_url, 'eap', gettext('EAP'))

    # ===================================== EAP adapters END =============================

    @chatbot_command_handler('disclaimer')
    def _disclaimer(self, title, body, positive_button_text='', negative_button_text=''):
        self.make_and_send_x_control(DisclaimerElement, title=title, body=body,
                                     positive_button_text=positive_button_text,
                                     negative_button_text=negative_button_text)

    @chatbot_command_handler('stripe_popup')
    def _stripe_popup(self, purpose=''):
        purpose = purpose.lower()
        self.make_and_send_x_control(
            StripePopupElement,
            title=gettext('Payment Information'),
            user=self.user,
            amount=purpose == str('telemedicine' and self.user.subscription_consult_fee or '0.00')
        )

    @chatbot_command_handler('refresh_web_views')
    def _refresh_web_views(self):
        self.send_to_chat(RefreshWebViewsElement())

    @chatbot_command_handler('set_animation_speed', callback_on_error=True)
    def _set_animation_speed(self, speed):
        from healthjoy.auth.utils import get_animation_speed_config

        animation_speed_config = get_animation_speed_config()
        assert speed in animation_speed_config
        self.user.animation_speed_type = speed
        db.session.commit()
        self.say(AnimationSpeedElement(self.user.animation_speed))

    @chatbot_command_handler('blank_control')
    def _blank_control(self):
        self.say(WrongXControlCount())

    @chatbot_command_handler('create_fake_cards')
    def _create_fake_cards(self):
        user = self.user
        if user.campaign.is_acme_demo:
            fake_js = ACME_DEMO
        elif user.campaign.alias in CAMPAIGN_FAKE_JOURNEYS:
            fake_js = CAMPAIGN_FAKE_JOURNEYS[user.campaign.alias] or []
        else:
            from healthjoy.import_tool.extra import PredefinedCardsStorage
            fake_js = [j_data for j_data in PredefinedCardsStorage(self.user.id)]
        for fake_journey_params in fake_js:
            _journeys[fake_journey_params['controller']].make_fake_journey(user, fake_journey_params)

    @chatbot_command_handler('schedule_push', callback_on_error=True, translate_args=('push_text',))
    def _schedule_push(self, delay=None, push_text=None):
        delay = delay or 900
        push_text = push_text or gettext(u'✋ Hi there. Open the app to complete your request.')
        schedule_push(self.user, push_text, category=APS_CATEGORY_NOT_CHAT, delay=int(delay))

    @chatbot_command_handler('cancel_scheduled_pushes')
    def _cancel_scheduled_pushes(self):
        cancel_scheduled_pushes(self.user)

    @chatbot_command_handler('set_has_pcp', with_callback=True)
    def set_has_pcp(self, profile_id=None, has_pcp=None, name=None):
        if profile_id:
            profile_id = profile_id.replace(',', '')
            profile = UserProfile.query.get(profile_id)
            assert profile and profile.main_user == self.user
        else:
            profile = self.user.profile

        pcp_record = profile.pcp_record or UserProfilePCPRecord(profile_id=profile.id,
                                                                source=ASSESSMENT_SOURCE_MEMBER_INPUT)
        pcp_record.has_pcp = bool(has_pcp)
        pcp_record.name = name or pcp_record.name
        db.session.add(pcp_record)
        db.session.commit()

    @chatbot_command_handler('get_tz', callback_on_error=True)
    def _get_tz(self, coords):
        # TODO: GEO#3 flow google [REFACTORED]
        lat, lon = coords.split(",")
        timezone_info = geo_service_client.get_timezone_info(lat, lon)
        self._delegate_to_chatbot(timezone_info['timeZoneId'] if timezone_info else 'error')

    @chatbot_command_handler('get_ticket_link')
    def _get_ticket_link(self, journey_id):
        from healthjoy.crm import get_ticket_link

        journey_id = journey_id.replace(',', '')
        state = JourneyState.query.get(journey_id)
        self._delegate_to_chatbot(get_ticket_link(state.ticket_id))

    @chatbot_command_handler('is_profile_activated', with_callback=True, report_error=False)
    def _is_profile_activated(self, profile_id):
        profile_id = int(profile_id.replace(',', ''))
        profile = self.user.get_profile_by_id(profile_id)
        assert profile and profile.main_user and profile.main_user.active

    @chatbot_command_handler('get_profile_email', callback_on_error=True, report_error=False)
    def _get_profile_email(self, profile_id):
        profile_id = int(profile_id.replace(',', ''))
        profile = self.user.get_profile_by_id(profile_id)
        assert profile and profile.main_user and profile.main_user.email and not profile.main_user.is_tmp_email
        self._delegate_to_chatbot(profile.main_user.email)

    # ===================================== Appointment adapters =============================
    def _get_user_providers(self, profile_id):
        if isinstance(profile_id, basestring):
            profile_id = profile_id.replace(',', '')
        profile = self.user.get_profile_by_id(profile_id)
        providers = profile.active_providers
        if profile.has_pcp and profile.pcp_record.active and profile.pcp_record.name:
            providers.append(profile.pcp_record)
        return providers

    @chatbot_command_handler('user_providers_count')
    def _user_providers_count(self, profile_id):
        if not profile_id:
            profile_id = self.env.get('appointment_profile_id')

        self._delegate_to_chatbot(str(len(self._get_user_providers(profile_id))))

    @chatbot_command_handler(
        'simple_providers_list', translate_args=('placeholder', 'header', 'default_text')
    )
    def _simple_providers_list(self, default_text=None, placeholder=None, header=None, expand=False,
                               default_first=None):
        try:
            self._user_providers_list(
                self.user.profile.id, default_text=default_text, placeholder=placeholder,
                header=header, expand=expand, default_first=default_first
            )
        except KeyError:
            self._delegate_to_chatbot('error')
        else:
            self._move_to_autocomplete_state()

    @chatbot_command_handler('user_providers_list', translate_args=('placeholder', 'header', 'default_text'))
    def _user_providers_list(self, profile_id=None, default_text=None, placeholder=None, header=None, expand=False,
                             default_first=None):
        if not profile_id:
            profile_id = self.env.get('appointment_profile_id')

        assert profile_id

        providers = self._get_user_providers(profile_id)

        self.send_x_control(make_select(
            self._state,
            default_text=default_text,
            default_first=default_first,
            choices=[(provider.id, provider.full_title) for provider in providers],
            title=placeholder,
            header=header,
            expand=expand,
            type_=SELECT_TYPE_STATIC_LIST
        ))

    @chatbot_command_handler('user_provider_info')
    def _user_provider_info(self, profile_id=None, provider_id=None):
        if not profile_id:
            profile_id = self.env.get('appointment_profile_id')

        assert profile_id

        provider = next(p for p in self._get_user_providers(profile_id) if p.id == int(provider_id.replace(',', '')))
        self._delegate_to_chatbot(provider and provider.name or '')

    def _get_future_appointments(self):
        appointments = list()
        for journey_state in JourneyState.query.filter(JourneyState.profile_id == self.user.assistant_profile.id,
                                                       JourneyState.controller == AppointmentJourney.__name__,
                                                       JourneyState.state.in_(AppointmentJourney.COMPLETED_STATES)):
            journey = journey_state.load_journey()
            dt = journey.appointment_dt and journey.appointment_dt.astimezone(pytz.UTC).replace(tzinfo=None)
            if dt and dt > datetime.utcnow():
                appointments.append(journey)
        return appointments

    @chatbot_command_handler('future_appointments_count')
    def _appointments_count(self):
        self._delegate_to_chatbot(str(len(self._get_future_appointments())))

    @chatbot_command_handler('future_appointments', translate_args=('placeholder', 'header', 'default_text'))
    def _future_appointments(self, default_text=None, placeholder=None, header=None, expand=False, default_first=None):
        appointments = {}
        for appointment in self._get_future_appointments():
            dt = appointment.user_appointment_dt
            if not dt:
                continue
            text = appointment.env.get('doctor_title')
            text = '{} on {}'.format(text, dt.strftime('%B %d, %Y, %I:%M %p'))
            appointments[dt] = (appointment.id, text.strip())

        appointments = [a for _, a in sorted(appointments.items())]

        self.send_x_control(make_select(
            self._state,
            default_text=default_text,
            default_first=default_first,
            choices=appointments,
            title=placeholder,
            header=header,
            expand=expand,
            type_=SELECT_TYPE_STATIC_LIST
        ))

    def _get_appointment(self, appointment_id):
        appointment = JourneyState.query.get(appointment_id.replace(',', ''))
        assert (
            appointment and appointment.controller == AppointmentJourney.__name__ and
            appointment.profile_id == self.user.assistant_profile.id
        )
        return appointment

    def _edit_appointment(self, appointment_id, ticket_subject, ticket_text):
        from healthjoy.crm import add_article_to_ticket, reopen_ticket

        appointment = self._get_appointment(appointment_id)
        add_article_to_ticket(appointment.ticket_id, self.user, ticket_subject, ticket_text)
        reopen_ticket(appointment.ticket_id)
        appointment.state = AppointmentJourney.STATES.NEW
        db.session.commit()

    @chatbot_command_handler('reschedule_appointment')
    def _reschedule_appointment(self, appointment_id, ticket_text):
        self._edit_appointment(appointment_id, 'Reschedule appointment', ticket_text)

    @chatbot_command_handler('cancel_appointment')
    def _cancel_appointment(self, appointment_id, ticket_text):
        self._edit_appointment(appointment_id, 'Cancel appointment', ticket_text)

    # ===================================== Acme demo adapters ============================
    @chatbot_command_handler('invite_demo_user')
    def _invite_demo_user(self, email, first_name, last_name, phone_number=None):
        from healthjoy.auth.user_management import create_demo_user

        if self.user.inviter_id:
            logger.warning('User %s tried to invite demo user %s, but he is invited', self.user.id, email)
            return

        logger.info('User %s is inviting demo user %s', self.user.id, email)
        user = create_demo_user(email, self.user.campaign, first_name, last_name, phone_number=phone_number)
        user.inviter_id = self.user.id
        db.session.commit()

        from healthjoy.icr.jobs import create_user
        create_user.delay(user.id, eta=timedelta(seconds=2))
        logger.info('Demo user %s created by user %s', user.id, self.user.id)

        from healthjoy.auth.jobs.salesforce import upload_demo_user_to_sf
        if self.user.salesforce_contact_id:
            upload_demo_user_to_sf.delay(user.id, self.user.salesforce_contact_id, eta=timedelta(seconds=2))

        zapier_hooks = json.loads(HealthJoyConfigs.get_value('ZAPIER_HOOKS', default="{}"))
        hook = zapier_hooks.get(self.user.campaign.alias, {}).get('demo_invite')
        if hook:
            logger.debug('Sending demo invite to zapier hook %s, user %s, inviter %s', hook, user.id, self.user.id)
            try:
                requests.get(hook.format(user=user, inviter=self.user))
            except requests.RequestException:
                sentry.captureException(sys.exc_info(), tags=dict(bundle=log.extract_bundle(user)))

    @chatbot_command_handler('family_plain', callback_on_error=True)
    def _family_plain(self):
        profiles = self.user.get_relatives(only_active=True, with_undefined=True).order_by(
            UserProfile.birthday.asc().nullslast()
        )
        self._delegate_to_chatbot(CHATBOT_LINEBREAK_TOKEN.join(map(u'{0.name} {0.second_name}'.format, profiles)))

    def prepare_doc_data(self, display_type, doctor):
        fields = PROVIDER_DATA_FIELDS.get(display_type, [])
        doctor_info = data_api.retrieve_doctor(doctor['npi'])
        doctor_data = dict(title=doctor.get('title'))
        gender = doctor.get('gender', '')
        doctor_data['gender'] = gender.lower() == 'm' and 'Male' or gender.lower() == 'f' and 'Female' or gender
        address = doctor.get("addresses") and doctor['addresses'][0] or {}
        full_address = address and address.get('checksum') and \
            data_api.retrieve_address_by_checksum('doctor', address['checksum']) or {}
        schools = list()
        for school in doctor_info.get('doctor_schools', []):
            if school.get('name'):
                schools.append(', '.join(map(str, filter(None, [school['name'], school.get('year')]))))
        doctor_data['schools'] = CHATBOT_LINEBREAK_TOKEN.join(schools)
        doctor_data['address'] = full_address.get('formatted_address', '')
        doctor_data['phone'] = full_address.get("display_phone", '')
        distance = float(address.get('distance'))
        doctor_data['distance'] = '{} miles'.format(str(round(distance, 1)))
        careington_address = next((c_address
                                   for c_address in doctor_info.get('doctor_addresses')
                                   if c_address.get('notes') and c_address['notes'].get('careington_office_name')),
                                  None) or {}
        if not careington_address and any(f in fields for f in ['careington_office_name', 'careington_phone']):
            sentry.captureMessage('No careington address for doctor {}'.format(doctor['npi']), suppress_sms=True)
        doctor_data['careington_office_name'] = careington_address.get('careington_office_name', '')
        doctor_data['careington_phone'] = careington_address.get('careington_phone', '')
        return CHATBOT_LINEBREAK_TOKEN.join([doctor_data.get(field, '') for field in fields])

    @chatbot_command_handler('provider_details', callback_on_error=True, report_error=False)
    def provider_details(self, source, npi, zip_code, display_type='preview'):
        npi = npi.replace(',', '')
        if source == 'doctor':
            doctor = data_api.search(source, npi, {'zip_code': zip_code})[0]
            self._delegate_to_chatbot(self.prepare_doc_data(display_type, doctor))
        else:
            raise ValueError('unknown provider type: {}'.format(source))

    @chatbot_command_handler('provider_list_details', callback_on_error=True, report_error=False)
    def provider_list_details(self, source, filters, slice_items, display_type='preview'):
        filters = self._tricky_parse_qsl(filters)
        items = data_api.search(source, filters.pop('query', ''), **filters)
        if not len(items):
            raise ValueError('No items found for: {} with filters {}'.format(source, filters))
        if slice_items:
            items = items[:int(slice_items)]

        delimiter = '{br}=================={br}'.format(br=CHATBOT_LINEBREAK_TOKEN)
        self._delegate_to_chatbot(delimiter.join([self.prepare_doc_data(display_type, item)
                                                  for item in items]))

    # ==========================================================================

    # ===================================== Telemedicine testing adapters ============================

    def _update_consultation_status(self, consultation_id, status, **kwargs):
        params = dict(Status=status, operation='update', ExternalIntakeID=str(consultation_id))
        params.update(**kwargs)
        resp = requests.post('{}/callbacks/memd/{}/status'.format(current_app.config['BACKEND_INTERNAL_DOMAIN'],
                                                                  consultation_id), json=params)
        assert resp

    @chatbot_command_handler('assign_consultation_provider', with_callback=True)
    def _assign_consultation_provider(self, provider_name):
        self._update_consultation_status(self.user.active_consultation.id, 'PA', Data=provider_name)

    @chatbot_command_handler('start_doctor_visit', with_callback=True)
    def _start_doctor_visit(self):
        self._update_consultation_status(self.user.active_consultation.id, 'VS')

    @chatbot_command_handler('finish_doctor_visit', with_callback=True)
    def _finish_doctor_visit(self):
        consultation = self.user.active_consultation_with_doctor
        self._update_consultation_status(consultation.id, 'VC')
        resp = requests.post('{}/callbacks/memd/{}/closeout'.format(current_app.config['BACKEND_INTERNAL_DOMAIN'],
                                                                    consultation.id),
                             json=dict(operation='closeout', Data={}))
        assert resp

    # ==========================================================================

    @chatbot_command_handler("send_find_care_feedback_link")
    def _send_find_care_feedback_link(self):
        survey_link = HealthJoyConfigs.get_value("FIND_CARE_SURVEY_URL")
        format_params = {
            "first_name": self.user.profile.name,
            "last_name": self.user.profile.second_name,
            "email": self.user.contact_email or self.user.email,
            "id": self.user.id,
        }
        if "search_id" in survey_link:
            try:
                search = provider_basket_client.get_users_latest_search_context(self.user.id)
            except ApiError:
                format_params["search_id"] = ""
            else:
                format_params["search_id"] = search["search_id"]

        survey_link = survey_link.format(**format_params)
        self.say(ConversationBrowseElement(survey_link))

    def handle_submit_when_greeting_sent(self, el):
        self.say(el, msg_id=el.id)
        solved, should_close_chat = ChatbotSelectHandler(self).process_tile_selected(el)
        self.track_journey_step(el, 'tiles')
        if solved:
            if should_close_chat:
                self._close_chat()
                return

            self.send_tiles()
            return

        self.state = self.STATES.CHATBOT_CONVERSATION
        self.save_context_obj(el.show_value)
        self._delegate_to_chatbot(el.chatbot_value)

    def handle_message_when_greeting_sent(self, message):
        self.state = self.STATES.CHATBOT_CONVERSATION
        self.get_help()

    def handle_submit_when_chatbot_conversation(self, el):
        self.say(el, msg_id=el.id)
        self.track_journey_step(el, 'step')
        self._delegate_to_chatbot(el.chatbot_value)

    def handle_submit_when_photo_upload(self, el):
        self.send_to_chat(el, msg_id=el.id)
        self.track_journey_step(el, 'photo')

        # crutch: old app sometimes send us select instead of photo
        if not isinstance(el, FileUploadElement) or not getattr(el, 'attachments', None):
            self.state = self.STATES.CHATBOT_CONVERSATION
            self._delegate_to_chatbot(el.DEFAULT)
            return

        flat_attachments = map(dict, el.attachments)
        attach_str = el.chatbot_value
        self.save_context_obj(dict(attachments=flat_attachments, attach_str=attach_str))

        self.state = self.STATES.CHATBOT_CONVERSATION
        self._delegate_to_chatbot(attach_str)

    def handle_message_when_photo_upload(self, message):
        self.state = self.STATES.CHATBOT_CONVERSATION
        attachments = message['attachments']
        to_delegate = ', '.join([a['url'] for a in attachments]) if attachments else PhotoElement.DEFAULT
        self._delegate_to_chatbot(to_delegate)

    def stop_conversation(self):
        self.cancel_postponed()
        self._delegate_to_chatbot('[abort dialog]', safe=False)

    def handle_submit_when_feedback(self, el):
        self.say(el, msg_id=el.id)

        survey_id = HealthJoyConfigs.get_value('SURVICATE_CSAT_ID')
        if not survey_id:
            logger.error('SURVICATE_CSAT_ID config is missing during handling feedback')
            self._close_chat()
            return

        survey_link = (
            'https://survey.survicate.com/{survey_id}/?{query_string}'
        )
        previous_thread = self.thread.get_previous_closed()
        pha = previous_thread.pha
        if not pha:
            logger.error('Can not find pha for feedback')
            self._close_chat()
            return

        user_profile = self.user.profile
        survey_data = dict(
            p='customerio',
            first_name=user_profile and user_profile.name,
            last_name=user_profile and user_profile.second_name,
            email=self.user.contact_email or self.user.email,
            uid=self.user.id,
            agent_id=pha.id,
            agent_name=pha.full_name,
            campaign_id=self.user.campaign_id,
            campaign_name=self.user.group_name,
            communication_id=self.thread.id,
            aid=el.value,
            channel='chat',
        )
        self.say(ConversationBrowseElement(
            survey_link.format(survey_id=survey_id, query_string=urlencode(survey_data))
        ))
        logger.info('Survey link was send', extra={'thread_id': self.thread.id})
        self._close_chat()

    def send_feedback(self):
        survey_id = HealthJoyConfigs.get_value('SURVICATE_CSAT_ID')
        answers = json.loads(HealthJoyConfigs.get_value('SURVICATE_CSAT_ANSWERS', default="[]"))
        if not answers or not survey_id:
            logger.warning('Survicate id and answers should be configured to collect chat feedback')
            raise KeyError('Config for survey is not found')
        try:
            choices = (
                (answer['id'], answer['title'])
                for answer in answers
            )
        except KeyError:
            logger.error('Invalid SURVICATE_CSAT_ANSWERS config')
            raise KeyError('Config for survey answers is incorreclty formatted')
        message = "Your feedback is important to us. Please rate your live chat experience."
        self.say(message)
        self.send_x_control(make_select(
            self._state,
            choices=choices,
            title=message,
            type_=SELECT_TYPE_STATIC_LIST,
        ))
        logger.info('Chat feedback was send')
        self.state = self.STATES.FEEDBACK


class ChatbotChildJourney(ChatbotBaseJourney):
    journey_type = ''
    title = ''
    widget_type = 'card'
    COMPLETED_STATES = tuple()

    def stop_conversation(self):
        self.state = self.STOP_STATE
        parent_journey = self._actualize_parent_journey()
        parent_journey.stop_conversation()

    def __init__(self, *args, **kwargs):
        super(ChatbotChildJourney, self).__init__(*args, **kwargs)
        # assert self._state.parent.controller == 'ChatbotJourney'

    @classmethod
    def create(cls, thread, parent=None, activate=True, start=True, **kwargs):
        dynamic_fields = kwargs.get('dynamic_fields') or {}
        created_by = dynamic_fields.get('createdby', 'Joy')
        j = super(ChatbotChildJourney, cls).create(
            thread, parent=parent,
            activate=activate, start=start, **kwargs
        )
        logger.info(
            'Journey {} for thread {} creatd by {}'.format(j.id, thread.id, created_by),
            extra=dict(
                event='journey_start', user=thread.owner_id, thread_id=thread.id,
                traits={
                    'j_id': j.id,
                    'type': j.journey_type,
                    'created_by': created_by
                }
            )
        )
        return j

    def archive(self):
        thread_id = self.thread_id
        state = self.state
        self.logger.info(
            'Journey {} for thread {} completed with status {}'.format(self.id, thread_id, state),
            extra=dict(
                event='journey_complete',
                traits={
                    'j_id': self.id,
                    'type': self.journey_type,
                    'state': state
                }
            )
        )
        return super(ChatbotChildJourney, self).archive()

    def _send_journey_presentation(self, state=None, campaign_type=''):
        self.send_to_chat(JourneyPresentationElement(self, status=state), campaign_type=campaign_type)

    def get_widget_type(self):
        return self.widget_type or self.journey_type

    def get_parent_journey(self, **extra_kwargs):
        return BaseJourney.load(self._state.parent, **extra_kwargs)

    def _actualize_parent_journey(self, **extra_kwargs):
        parent_journey = self.get_parent_journey(**extra_kwargs)
        parent_journey.update_thread(self.thread, update_state=True, silent=True)
        return parent_journey

    def _handle_chatbot_command(self, command, *orig_args, **orig_kwargs):
        if self._have_chatbot_command(command):
            return super(ChatbotChildJourney, self)._handle_chatbot_command(command, *orig_args, **orig_kwargs)

        parent_journey = self._actualize_parent_journey(x_controller=self.x_controller)
        return parent_journey._handle_chatbot_command(command, *orig_args, **orig_kwargs)

    @chatbot_command_handler('icr_card')
    def _icr_card(self, campaign_type=''):
        self._send_journey_presentation(state=self.STOP_STATE, campaign_type=campaign_type)

    @BaseJourney.state.setter
    def state(self, new_state):
        from ..jobs import send_journey_update
        old_state = self.state
        super(ChatbotChildJourney, self)._set_state(new_state)
        if new_state != old_state:
            journey_id = self._state.id
            if journey_id:
                send_journey_update.delay(self._state.id, eta=timedelta(seconds=5))


class ChatbotChildSimpleJourney(ChatbotChildJourney):
    DEFAULT_TITLE = None

    STATES = enum(
        'MVP_STATES',
        NEW='in progress',
        RESOLVED='completed',  # Answered
        CANCELLED='cancelled'
    )
    INIT_STATE = STATES.NEW
    STOP_STATE = STATES.RESOLVED

    def start(self):
        db.session.commit()
        from healthjoy.icr.jobs import send_journey_update
        send_journey_update(self)

    def handle_ticket_created(self):
        self.logger.info("Ticket #%s has been created for journey: %s", self._state.ticket_id, self.id)

    def update_card_on_delivery(self):
        pass

    @property
    def ticket_default_subject(self):
        user = self.user
        category = ' '.join(filter(None, set([self.DEFAULT_TITLE, getattr(self, '_title', None)])))
        return u'AUTODELIVERY {bundle} {campaign} {category}'.format(
            campaign=user.campaign.title,
            bundle=user.bundle.title,
            category=category
        )

    @property
    def title(self):
        return getattr(self, '_title', None) or self.DEFAULT_TITLE

    def _fill_card(self, card_response, resolved_by=None, resolved_by_name=None):
        self.set_env('card_response', card_response)
        self.set_env('unread', True)

        if resolved_by:
            self.set_env('resolved_by', resolved_by)
        if resolved_by_name:
            self.set_env('resolved_by_name', resolved_by_name)
        db.session.commit()
        notify_card_updated_push.delay(self.user.id)

    def _get_card_delivery_type(self):
        return self.user.pushable_devices and 'card_delivery_push' or 'card_delivery_no_push'

    def deliver(self):
        channel_manager = DeliveryChannel(
            self.thread,
            message_timestamp=time.time(),
            campaign_type=self._get_card_delivery_type(),
            params=dict(widget_id=self.id, widget_type=self.widget_type, widget_title=smart_str(self.title))
        )
        channel_manager.plan_next_channel(remember_job=False)
        self.set_env('delivery_sent', True)
        db.session.commit()


class ChatbotGeneralCardJourney(ChatbotChildSimpleJourney):
    STATES = enum('CARD_STATES', **JOURNEY_CARD_STATES)
    DEFAULT_STATE = STATES.FOLLOWUP
    COMPLETED_STATES = (STATES.FOLLOWUP, STATES.RESOLVED)
    chatbot_delivery_alias = 'simple'
    campaign_type = 'journey_delivery'
    CUSTOM_TITLES = tuple()
    DYNAMIC_FIELDS = tuple()

    def __init__(self, *args, **kwargs):
        custom_title = kwargs.pop('journey_title', None)
        super(ChatbotGeneralCardJourney, self).__init__(*args, **kwargs)
        self._title = custom_title or self.env.get('title', self.DEFAULT_TITLE)

    def start(self):
        super(ChatbotGeneralCardJourney, self).start()
        self.set_env('title', self._title)
        db.session.commit()

    def _set_extra_context(self, **extra_context):
        context_ = self.env.get('extra_context', {})
        context_.update(**extra_context)
        self.set_env('extra_context', context_)
        db.session.commit()

    @property
    def profile_name(self):
        requested_for_id = self._state.parameters.get('requested_for_id')
        profile = requested_for_id and UserProfile.query.get(requested_for_id)
        return profile and profile.full_name or self.user.profile.full_name

    def _get_extra_context(self, **kwargs):
        return dict(journey_title=self.title, journey_type=self.journey_type)

    @property
    def _ready_to_view_key(self):
        return JOURNEY_READY_TO_VIEW_KEY % self.id

    def unmark_ready_to_view(self):
        redis.delete(self._ready_to_view_key)

    def mark_as_ready_to_view(self, permanent=True):
        from healthjoy.icr.jobs import send_journey_update
        if permanent:
            redis.set(self._ready_to_view_key, value=1)
            self.set_env('in_delivery', False)
        else:
            # change state as soon as possible
            redis.delete(self._ready_to_view_key)
            send_journey_update.delay(self.id, eta=None)

    @property
    def is_ready_to_view(self):
        return bool(redis.get(self._ready_to_view_key))

    def handle_submit_when_followup(self, el):
        return self.handle_default_submit(el)

    def _get_delivery_fire_command(self):
        delivery_alias = self.chatbot_delivery_alias or self.journey_type
        return '[{} delivery]'.format(delivery_alias)

    def _process_state_delivery(self, fire_bot=True, **kwargs):
        prev_state = self.state

        # card_question handling for backward compatibility with old queued cards
        card_question = kwargs.pop('card_question', None)
        if 'card_response' in kwargs:
            kwargs.update(
                card_response='<br/><br/>'.join(filter(None, [card_question, kwargs['card_response']]))
            )

        self._fill_card(**kwargs)

        extra_context = self._get_extra_context(**kwargs)
        self._set_extra_context(**extra_context)
        if fire_bot:
            company = self.get_journey_state().user.campaign
            if company.outbounds_enabled:
                fire_command = self._get_delivery_fire_command()
                self._delegate_to_chatbot(
                    fire_command,
                    safe=False,
                    extra_context=extra_context,
                    campaign_type=self.campaign_type
                )

        self.refresh_state()
        self.mark_as_ready_to_view()
        db.session.commit()

        ChatbotJourney.force_message_to_active_thread(self.user, InboxUpdatedNotificationElement(), to_chat=True)

        if self.state == prev_state:
            self.process_state(self.STOP_STATE)
            return self.STOP_STATE

    def _process_state_cancelled(self):
        self.archive()

    @chatbot_command_handler('start_followup')
    def _start_followup(self):
        self.update_thread(self.user.last_chat, silent=True)
        self.activate()
        self.state = self.STATES.FOLLOWUP
        db.session.commit()
        with AutocloseManager(self.thread_id) as autoclose:
            autoclose.cancel()

    @property
    def need_followup(self):
        return self.state == self.STATES.FOLLOWUP

    @chatbot_command_handler('complete_followup')
    def _complete_followup(self):
        self.state = self.STOP_STATE
        db.session.commit()

    def _handle_thread_closed(self):
        if self.state in self.COMPLETED_STATES and self.state != self.STOP_STATE:
            self.archive()

    @classmethod
    def make_fake_journey(cls, user, params, state=None):
        parameters = params.get('parameters', {})
        params['parameters'] = parameters
        params['parameters']['fake'] = True
        params['parameters']['unread'] = True
        created = False
        if state is None:
            created = True
            chat_profile = user.chat_profile
            parent_id = JourneyState.query.filter_by(
                profile_id=chat_profile.id,
                controller=ChatbotJourney.__name__
            ).first().id
            state = JourneyState(
                profile_id=chat_profile.id,
                controller=params['controller'],
                thread_id=user.last_chat.id,
                parent_id=parent_id
            )
            state.active = False
            state.state = cls.STOP_STATE

        state.parameters = params['parameters']

        if created:
            db.session.add(state)

        db.session.commit()
        if getattr(cls, 'use_decision_center', None) and 'card' in params:
            data = request_data_to_multidict(params['card'], nested_separator='-')
            cls.decision_card_cls.process(data, state, create=True)
        return state

    def handle_card_read(self):
        pass


class ChatbotChildSimpleCardJourney(ChatbotGeneralCardJourney):
    ticket_default_priority = 3
    crm_aux_args = []
    use_decision_center = False
    decision_card_type = None
    decision_card_cls = None
    decision_center_url_prefix = "decision_center"

    @property
    def _ticket_subject(self):
        ticket_subject = self._ticket_subj or self.env.get('ticket_subject') or self.ticket_default_subject
        self._ticket_subj = ticket_subject.format(campaign=self.user.campaign.title,
                                                  bundle=self.user.bundle.title)
        return self._ticket_subj

    def __init__(self, *args, **kwargs):
        ticket_details = kwargs.pop('ticket_details', '')
        ticket_priority = kwargs.pop('ticket_priority', None)
        ticket_subject = kwargs.pop('ticket_subject', None)
        dynamic_fields = kwargs.pop('dynamic_fields', None)
        super(ChatbotChildSimpleCardJourney, self).__init__(*args, **kwargs)
        self._ticket_details = ticket_details or self.env.get('ticket_details')
        self._ticket_priority = ticket_priority or self.env.get('ticket_priority', self.ticket_default_priority)
        self._ticket_subj = ticket_subject
        self._dynamic_fields = dynamic_fields or self.env.get('dynamic_fields')

    @property
    def dynamic_fields(self):
        return self._dynamic_fields or {}

    @property
    def card(self):
        return self._state.card

    @property
    def profile_name(self):
        card = self.card
        profile_name = card and card.profile_name
        if profile_name:
            return profile_name

        return super(ChatbotChildSimpleCardJourney, self).profile_name

    @classmethod
    def get_cards_cls(cls):
        if not cls.decision_card_cls or not cls.journey_type:
            return
        return DECISION_CARD_CLASSES.get(cls.journey_type)

    @classmethod
    def check_available_for_demo(cls):
        return cls.__name__ in ACME_DEMO_JOURNEYS

    def start(self):
        super(ChatbotChildSimpleCardJourney, self).start()
        self.set_env('ticket_details', self._ticket_details)
        self.set_env('ticket_priority', self._ticket_priority)
        self.set_env('dynamic_fields', self.dynamic_fields)
        self.set_env("intake_parameters", parse_chatbot_intake(self._ticket_details))
        db.session.commit()
        if self.use_decision_center:
            decision_url = get_decision_center_url(self)
            self._ticket_details = u'Decision center URL: {url}\n{0}'.format(
                self._ticket_details,
                url=decision_url
            )
            self.set_env('ticket_details', self._ticket_details)
            db.session.commit()

        if self.user.campaign.is_demo_only and self.check_available_for_demo():
            from healthjoy.icr.jobs import resolve_fake_journey
            resolve_fake_journey.delay(self.id)
            return

        self._create_otrs_ticket(self._ticket_subject, self._ticket_details,
                                 priority=self._ticket_priority,
                                 dynamic_fields=self.dynamic_fields)

    def _get_extra_context(self, **kwargs):
        extra_context = super(ChatbotChildSimpleCardJourney, self)._get_extra_context(**kwargs)
        extra_context.update(card_response=kwargs.get('card_response', ''))
        return extra_context


class PreCertEventJourney(ChatbotChildSimpleCardJourney):
    class FireCommands(object):
        HCC = '[tpa_precert_eventbad_decision]'
        MEMBER = '[tpa_precert_event]'
        UNKNOWN = '[tpa_precert_unknown_decision]'
        NO_REWARD = '[tpa_precert_no_reward]'

    @property
    def pre_cert_decision(self):
        card = self.card
        decisions = card and card.decisions
        if not decisions:
            return None
        decision = decisions[0]
        if not isinstance(decision, (ProviderDecision, FacilityDecision)):
            return None
        if not decision.is_pre_cert_event:
            return None
        return decision

    def _get_card_delivery_type(self):
        delivery_type = super(PreCertEventJourney, self)._get_card_delivery_type()

        # Workaround for sending email on pre-certification events
        if self.pre_cert_decision:
            logger.info(
                'PreCertEventJourney - set delivery type to card_delivery_no_push for user: %s',
                self.user.id
            )
            delivery_type = 'card_delivery_no_push'

        return delivery_type

    def _get_delivery_fire_command(self):
        fire_command = self.env.get('pre_cert_intake_journey')
        if not fire_command:
            fire_command = super(PreCertEventJourney, self)._get_delivery_fire_command()
        return fire_command

    def _get_fire_command_for_decision(self, decision=None):
        decision = decision or self.pre_cert_decision
        if not decision.pre_cert_reward_amount:
            return self.FireCommands.NO_REWARD
        if decision.pre_cert_decision == 'hcc':
            return self.FireCommands.HCC
        if decision.pre_cert_decision == 'member':
            return self.FireCommands.MEMBER
        return self.FireCommands.UNKNOWN

    def deliver(self):
        user_id = self.user.id
        logger.info('PreCertEventJourney - start deliver for user: %s', user_id)

        decision = self.pre_cert_decision
        if decision and not self.env.get('pre_cert_intake_journey'):
            fire_command = self._get_fire_command_for_decision(decision)
            self.set_env('pre_cert_intake_journey', fire_command)
            db.session.commit()

            logger.info('PreCertEventJourney - send %s push for user: %s',
                        fire_command, user_id)
            push_text = MessageText.get_user_value(self.user, 'PRE_CERT_PUSH_TEXT', default=None)
            if push_text:
                send_mobile_push.delay(user_id, push_text, update_thread=True)

        logger.info('PreCertEventJourney - call super deliver for user: %s', user_id)
        super(PreCertEventJourney, self).deliver()


class QnAJourney(ChatbotChildSimpleCardJourney):
    DEFAULT_TITLE = 'Message'
    CUSTOM_TITLES = ('Question', 'Insurance Topic', 'Medical Topic', 'Message')
    journey_type = view_type = 'message'
    crm_category = 'Message'

    use_decision_center = True
    decision_card_type = 'message'
    decision_card_cls = MessageCard
    widget_type = 'message_card'


class RxSavingsJourney(ChatbotChildSimpleCardJourney):
    DEFAULT_TITLE = 'Rx Savings'
    CUSTOM_TITLES = (DEFAULT_TITLE,)
    journey_type = 'rx_savings'
    view_type = 'money'
    crm_category = 'Rx Savings'

    use_decision_center = True
    decision_card_type = 'rx'
    widget_type = 'rx_card'
    decision_card_cls = RXCard

    @property
    def ticket_default_subject(self):
        subject = super(RxSavingsJourney, self).ticket_default_subject
        try:
            walmart_list = json.loads(HealthJoyConfigs.get_value('WALMART_$4_LIST', default='[]'))
        except (TypeError, ValueError):
            walmart_list = []

        medications = self.medications
        if medications and medications.issubset(walmart_list):
            subject = '{} Walmart Formulary'.format(subject)

        return subject

    @property
    def medications(self):
        return {medication.get('name', '').lower().strip() for medication in self.env.get('rx_medications', [])}

    @property
    def dynamic_fields(self):
        dynamic_fields = super(RxSavingsJourney, self).dynamic_fields
        medications = self.medications
        # OTRS doesn't support numeric dynamic fields and can't apply logic to them
        dynamic_fields["medicationcount"] = "<3" if len(medications) < 3 else "3+"
        return dynamic_fields

    def _process_state_followup(self):
        self._start_followup()
        self._delegate_to_chatbot('[rx_followup]', safe=False)

    def _process_state_delivery(self, **kwargs):
        from healthjoy.icr.jobs import add_journey_to_delivery

        state = super(RxSavingsJourney, self)._process_state_delivery(**kwargs)
        if not self.user.campaign.is_acme_demo and self.card and self.card.has_savings:
            delay = current_app.config.get('DEV_OR_STAGE') and 60 or 30 * 24 * 3600
            add_journey_to_delivery.delay(self.id, self.STATES.FOLLOWUP, should_resurrect=True,
                                          eta=timedelta(seconds=delay))
        return state

    def deliver(self):
        if self.state == self.STOP_STATE:
            push_text = MessageText.get_user_value(self.user, 'RX_FOLLOWUP_PUSH_TEXT', '')
            if push_text:
                send_mobile_push(self.user, text=push_text, check_permission=True)
        else:
            super(RxSavingsJourney, self).deliver()


class JourneyWithRedeemMixin(object):

    def _process_state_completed(self):
        card = self.card
        decision = card and card.recommended_decision
        reward_amount = decision and decision.reward_amount
        self.logger.warning('redeem: in {} process state completed, journey id {}, {}, {}, {}'.format(
            self.journey_type, self.id, bool(card), bool(decision), bool(reward_amount)
        ))
        if reward_amount and self._check_redeem_needed():
            self.logger.warning('redeem: adding {} redeem journey, journey id {}'.format(
                self.journey_type, self.id
            ))
            state = self._get_or_create_redeem_state()
            db.session.add(state)
            db.session.commit()
            self.logger.warning('redeem: {} redeem journey state added, journey id {}'.format(
                self.journey_type, self.id
            ))

    def _get_or_create_redeem_state(self):
        state = JourneyState(state=RedeemJourney.INIT_STATE, controller=RedeemJourney.__name__,
                             thread_id=self.thread_id, parent_id=self.id, profile=self._state.profile)
        return state

    def _check_redeem_needed(self):
        return not RedeemJourney.get_redeem_journey_state_for_parent(self.id)


class ProviderJourney(PreCertEventJourney, JourneyWithRedeemMixin):
    DEFAULT_TITLE = 'Provider'
    CUSTOM_TITLES = (
        'Dental or Dental Specialist', 'Primary Care Provider', 'Obstetrics & Gynecology',
        'Medical Specialist', 'Mental Health Provider', 'Surgeon',
        'Vision Provider', DEFAULT_TITLE
    )
    STATES = enum('CARD_STATES', **dict(JOURNEY_CARD_STATES, APPOINTMENT='appointment'))
    COMPLETED_STATES = ChatbotChildSimpleCardJourney.COMPLETED_STATES + (STATES.APPOINTMENT,)
    journey_type = 'provider'
    view_type = 'note'
    crm_category = 'Provider'

    use_decision_center = True
    decision_card_type = 'provider'
    widget_type = 'provider_card'
    decision_card_cls = ProviderCard

    def _process_state_appointment(self):
        self.update_thread(self.user.last_chat, silent=True)
        self.activate()
        self._delegate_to_chatbot('[appointment]', safe=False)

    def _process_state_cancelled(self):
        super(ProviderJourney, self)._process_state_cancelled()
        RedeemJourney.cancel_redeem_journey_for_parent(self.id)
        db.session.commit()

    def handle_submit_when_appointment(self, el):
        return self.handle_default_submit(el)


class FindCareJourneyMixin(object):
    """Common methods and attributes for Find Care journeys"""
    use_decision_center = True
    decision_center_url_prefix = "v2/decision_center"
    service_request_type = None

    def start(self):
        super(FindCareJourneyMixin, self).start()
        if not self.env.get("service_request_id"):
            response = service_requests_client.dc_create_service_request(
                payload={
                    "type": self.service_request_type,
                    "legacy_user_id": self.user.id,
                    "requested_for": self._state.parameters["requested_for_id"],
                    "source": "crm",
                    "status": "requested",
                    "details": self._state.parameters["ticket_details"],  # todo: DC link will be in "ticket_details"
                    "ticket_id": self._state.ticket_id,
                    "updated_by": self._state.parameters["dynamic_fields"]["createdby"],
                }
            )
            self.set_env("service_request_id", response["id"])
            if not self.env.get("initial_location"):
                self.set_env("initial_location", self.user.profile.location_data)
            db.session.commit()

    def handle_ticket_created(self):
        super(FindCareJourneyMixin, self).handle_ticket_created()
        service_request_id = self.env.get("service_request_id")
        if service_request_id:
            service_requests_client.update_service_request(
                service_request_id=service_request_id,
                payload={'ticket_id': self._state.ticket_id},
            )


class AppointmentMixin(object):
    """Mixin which handle logic for user notifications if appointment was scheduled, rescheduled or canceled."""

    DEFAULT_TITLE = "Appointment"
    view_type = 'sch_app'
    DYNAMIC_FIELDS = ('appointmentdt', 'doctortitle')
    decision_card_type = 'appointment'
    use_decision_center = True

    def get_otrs_appointment_dt(self):
        appointment_dt_str = (self.env.get('appointment_dt') or '').strip()
        return appointment_dt_str and dateparser_parse(appointment_dt_str,
                                                       settings={'TIMEZONE': 'America/Chicago',
                                                                 'RETURN_AS_TIMEZONE_AWARE': True})

    @property
    def user_appointment_dt(self):
        if self.card and self.card.local_dt:
            return self.card.local_dt

        if self.appointment_dt:
            zip_info = geo_service_client.get_zip_info(zip_code=self.user.profile.zip)
            user_tz_name = zip_info and zip_info['timezone'] or 'America/Chicago'
            return self.appointment_dt.astimezone(pytz.timezone(user_tz_name))

    @property
    def appointment_dt(self):
        if hasattr(self, '_appointment_dt') and self._appointment_dt:
            return self._appointment_dt
        card = self.card
        _date = None
        if isinstance(card, (AppointmentCard, FindCareAppointmentCard)):
            _date = card.local_dt
        else:
            _date = self.get_otrs_appointment_dt()
        setattr(self, '_appointment_dt', _date)
        return self._appointment_dt

    def _fill_card(self, card_response, resolved_by=None, resolved_by_name=None, appointmentdt=None,
                   doctortitle=None):
        super(AppointmentMixin, self)._fill_card(
            card_response, resolved_by=resolved_by, resolved_by_name=resolved_by_name
        )

        self.set_env('appointment_dt', appointmentdt)
        self.set_env('doctor_title', doctortitle)
        db.session.commit()

        job_ids = list()

        if self.appointment_dt:
            appointment_dt = self.user_appointment_dt
            appointment_dt_string = appointment_dt.isoformat()
            if appointment_dt_string == self.env.get('appointment_dt_scheduled'):
                # no need to do anything if appointment date was not changedk
                return

            self.set_env('appointment_dt_scheduled', appointment_dt_string)
            db.session.commit()

            day_notification_dt = appointment_dt.replace(hour=11, minute=0) - timedelta(days=1)
            hour_notification_dt = appointment_dt - timedelta(hours=1)
            day_notification_text = MessageText.get_user_value(self.user, 'DAY_APPOINTMENT_NOTIFICATION_TEXT', None)
            hour_notification_text = MessageText.get_user_value(self.user, 'HOUR_APPOINTMENT_NOTIFICATION_TEXT', None)
            pushes = {day_notification_text: day_notification_dt, hour_notification_text: hour_notification_dt}
            for push_text, push_dt in pushes.iteritems():
                if push_text:
                    push_dt = push_dt.astimezone(pytz.UTC).replace(tzinfo=None)
                    job = send_mobile_push.delay(self.user.id,
                                                 push_text.format(doctor_title=self.env['doctor_title']),
                                                 category=APS_CATEGORY_NOT_CHAT,
                                                 eta=push_dt)
                    job_ids.append(job.id)

            post_notification_text = MessageText.get_user_value(self.user, 'POST_APPOINTMENT_NOTIFICATION_TEXT', None)

            from healthjoy.icr.jobs import start_intake_journey_for_user, clear_intake_from_delivery

            clear_intake_from_delivery(self.user.id)

            if current_app.config.get('DEV_OR_STAGE'):
                post_notification_dt = datetime.now() + timedelta(minutes=2)
            else:
                post_notification_dt = appointment_dt.replace(hour=20).astimezone(pytz.UTC).replace(tzinfo=None)

            job = start_intake_journey_for_user.delay(
                self.user.id, '[post_appointment {}]'.format(self._state.id),
                push_text=post_notification_text.format(doctor_title=self.env['doctor_title']),
                clear_intake=True,
                eta=post_notification_dt
            )
            job_ids.append(job.id)

        self.refresh_state()
        self._cancel_push_jobs()
        if job_ids:
            self.set_env('push_job_ids', job_ids)
        db.session.commit()

    def _cancel_push_jobs(self):
        for job_id in self.env.get('push_job_ids', []):
            cancel_job(job_id)
        self.set_env('push_job_ids', [])

    def _process_state_cancelled(self):
        super(AppointmentMixin, self)._process_state_cancelled()
        self._cancel_push_jobs()
        db.session.commit()

    def _get_extra_context(self, **kwargs):
        extra_context = super(AppointmentMixin, self)._get_extra_context(**kwargs)
        extra_context.update(appointment_dt=self.env.get('appointment_dt', ''),
                             doctor_title=self.env.get('doctor_title', ''))
        return extra_context


class FindCareProviderJourney(FindCareJourneyMixin, ProviderJourney):
    DEFAULT_TITLE = 'Provider'
    CUSTOM_TITLES = (DEFAULT_TITLE, 'Facility')
    journey_type = 'find_care_provider'
    decision_card_cls = FindCareProviderCard
    widget_type = 'find_care_provider_card'
    crm_category = 'Find Care Provider'

    service_request_type = "provider"

    def update_card_on_delivery(self):
        super(FindCareProviderJourney, self).update_card_on_delivery()
        card = self.card
        if card and card.service_request_id:
            try:
                service_request = service_requests_client.dc_get_service_request(card.service_request_id)
            except ApiError as e:
                logger.exception("Failed to get service request %s: %s", card.service_request_id, str(e))
                return

            recommended_providers = get_recommended_providers(service_request["decisions"])
            if recommended_providers:
                card.recommended_npis = list(set(recommended_providers))
                db.session.commit()

    @property
    def pre_cert_decision(self):
        return None


class FindCareAppointmentJourney(FindCareJourneyMixin, AppointmentMixin, ChatbotChildSimpleCardJourney):
    journey_type = 'find_care_appointment'
    decision_card_cls = FindCareAppointmentCard
    widget_type = 'find_care_appointment_card'
    crm_category = 'Find Care Appointment'

    service_request_type = "appointment"


class SetInsuranceJourney(ChatbotChildSimpleCardJourney):
    DEFAULT_TITLE = 'Set Insurance'
    CUSTOM_TITLES = (DEFAULT_TITLE,)
    journey_type = 'set_insurance'
    view_type = 'timeless'
    crm_category = 'Set Insurance'


class NewFacilityJourney(PreCertEventJourney, JourneyWithRedeemMixin):
    DEFAULT_TITLE = 'Facility'
    CUSTOM_TITLES = ('Urgent Care', 'Laboratory', 'Imaging Center', 'Hospital', 'Rehabilitation Center', DEFAULT_TITLE)
    journey_type = 'new_facility'
    view_type = 'facility'
    crm_category = 'Facility'

    use_decision_center = True
    decision_card_type = 'facility'
    widget_type = 'facility_card'
    decision_card_cls = FacilityCard


class ChatSummaryJourney(ChatbotChildSimpleCardJourney):
    DEFAULT_TITLE = 'Chat Summary'
    CUSTOM_TITLES = (DEFAULT_TITLE,)
    journey_type = 'chat_summary'
    view_type = 'message'
    crm_category = 'Chat Summary'


class AppointmentJourney(AppointmentMixin, ChatbotChildSimpleCardJourney, JourneyWithRedeemMixin):
    DEFAULT_TITLE = 'Appointment'
    CUSTOM_TITLES = ('Facility Appointment', 'Provider Appointment')
    journey_type = 'appointment'
    crm_category = 'Appointment'

    widget_type = 'appointment_card'
    decision_card_cls = AppointmentCard

    @property
    def default_card(self):
        journey_state = self._state
        card_cls = journey_state.card_cls
        parent = journey_state.parent
        if not parent or parent.controller != ProviderJourney.__name__ or not parent.parameters:
            return

        decision_id = parent.parameters.get('selected_decision_id')
        decision = decision_id and ProviderDecision.query.get(decision_id)
        return decision and card_cls(journey_id=self.id, decision=decision) or None

    def _process_state_cancelled(self):
        super(AppointmentJourney, self)._process_state_cancelled()
        RedeemJourney.cancel_redeem_journey_for_parent(self.id)
        db.session.commit()

    def _get_or_create_redeem_state(self):
        parent_id = self._state.parent_id
        state = parent_id and RedeemJourney.get_redeem_journey_state_for_parent(parent_id)
        state = state or JourneyState(state=RedeemJourney.INIT_STATE, controller=RedeemJourney.__name__,
                                      thread_id=self.thread_id, profile=self._state.profile)
        state.parent_id = self.id
        return state

    def _check_redeem_needed(self):
        return True


class RedeemJourney(ChatbotChildSimpleCardJourney):
    DEFAULT_TITLE = 'Redeem'
    STATES = enum('CARD_STATES', **dict(
        JOURNEY_CARD_STATES,
        PROCESSING='processing',
        VERIFIED='verified',
        PAID='paid',
    ))
    COMPLETED_STATES = ChatbotChildSimpleCardJourney.COMPLETED_STATES + (STATES.PROCESSING,)
    journey_type = 'redeem'
    chatbot_delivery_alias = 'redeem'

    @chatbot_command_handler('icr_card')
    def _icr_card(self, campaign_type=''):
        self._send_chat_link('/views/rewards', 'savings_card', gettext('Rewards Center'))

    @classmethod
    def get_redeem_journey_state_for_parent(cls, parent_id):
        return JourneyState.query.filter_by(controller=cls.__name__, parent_id=parent_id).first()

    @classmethod
    def cancel_redeem_journey_for_parent(cls, parent_id):
        redeem_state = cls.get_redeem_journey_state_for_parent(parent_id)
        if redeem_state:
            redeem_journey = redeem_state.load_journey()
            redeem_journey.state = cls.STATES.CANCELLED

    def _change_redeem_status(self, state, bot_entry_notification=None):
        self.update_thread(self.user.last_chat, silent=True)
        self.activate()
        self.state = state
        db.session.commit()
        if bot_entry_notification is not None:
            self._delegate_to_chatbot(bot_entry_notification, safe=False)

    def start_redeem(self):
        self._change_redeem_status(self.STATES.NEW, bot_entry_notification='[redeem]')

    def _process_state_verified(self):
        self.activate()
        db.session.commit()
        self._delegate_to_chatbot('[redeemverified]', safe=False)

    def _process_state_paid(self):
        self.activate()
        db.session.commit()
        self._delegate_to_chatbot('[redeempaid]', safe=False)

    def _handle_thread_closed(self):
        if self.state in (self.STATES.PROCESSING, self.STATES.VERIFIED, self.STATES.PAID, self.STATES.NEW):
            self.deactivate()
            return
        super(RedeemJourney, self)._handle_thread_closed()

    def handle_submit_when_in_progress(self, el):
        return self.handle_default_submit(el)

    def handle_submit_when_paid(self, el):
        return self.handle_default_submit(el)

    def handle_submit_when_processing(self, el):
        return self.handle_default_submit(el)

    def handle_submit_when_verified(self, el):
        return self.handle_default_submit(el)

    def stop_conversation(self):
        prev_state = self.state
        super(RedeemJourney, self).stop_conversation()
        self.state = prev_state
        if self.state == self.STATES.PAID:
            self.finish_him()

    def deliver(self):
        push_text_key = None
        if self.state in [self.STOP_STATE, self.STATES.PAID]:
            push_text_key = 'REDEEM_PAID_PUSH_TEXT'
        elif self.state == self.STATES.VERIFIED:
            push_text_key = 'REDEEM_VERIFIED_PUSH_TEXT'

        push_text = push_text_key and MessageText.get_user_value(self.user, push_text_key, default=None)
        if push_text is None:
            super(RedeemJourney, self).deliver()
            return

        send_mobile_push(self.user, text=push_text, check_permission=True)

    @chatbot_command_handler('submit_redeem', with_callback=True)
    def _submit_redeem(self, ticket_subject=None, ticket_details=None, ticket_priority=None):
        self._ticket_subj = ticket_subject
        if isinstance(ticket_details, str):
            ticket_details = ticket_details.decode('utf-8')
        self._ticket_details = u'Parent Ticket Decision URL: {decision_url}\n\n{ticket_details}'.format(
            ticket_details=ticket_details,
            decision_url=get_decision_center_url(self._state.parent)
        )
        self._ticket_priority = ticket_priority or self.ticket_default_priority
        self.start()
        self.state = self.STATES.PROCESSING
        db.session.commit()


class TelemedJourney(ChatbotGeneralCardJourney):
    DEFAULT_TITLE = 'Medical Consultation'
    CUSTOM_TITLES = (DEFAULT_TITLE,)
    journey_type = 'telemed'
    view_type = 'consult'
    widget_type = 'telemed'

    def __init__(self, *args, **kwargs):
        consultation_id = kwargs.pop('consultation_id', None)
        super(TelemedJourney, self).__init__(*args, **kwargs)
        self._consultation_id = consultation_id or self.env.get('consultation_id')
        self.consultation = self._consultation_id and ConsultationQueue.query.get(self._consultation_id)

    @property
    def title(self):
        return self.consultation and self.consultation.title or self.DEFAULT_TITLE

    def start(self):
        super(TelemedJourney, self).start()
        self.set_env('consultation_id', self._consultation_id)
        db.session.commit()

    def _get_extra_context(self, **kwargs):
        extra_context = super(TelemedJourney, self)._get_extra_context(**kwargs)
        confirmation = self.consultation and self.consultation.confirmation
        extra_context.update(provider_name=confirmation and confirmation.get('ProviderName') or '')
        return extra_context

    def _fill_card(self, **kwargs):
        self.set_env('unread', True)
        db.session.commit()
        notify_card_updated_push.delay(self.user.id)

    @classmethod
    def load_telemed_journey(cls, consultation):
        s = next((j for j in JourneyState.query.filter_by(controller=cls.__name__,
                                                          profile_id=consultation.user.get_assistant_profile().id)
                  if j.parameters.get('consultation_id') == consultation.id), None)
        if not s:
            raise JourneyNotFound
        return cls.load(s)

    @property
    def profile_name(self):
        consult = self.consultation
        patient_name = consult and consult.patient_name
        if patient_name:
            return patient_name

        return super(TelemedJourney, self).profile_name

    @classmethod
    def make_fake_journey(cls, user, params, state=None):
        if state:
            # no need to do something with already created fake journey
            return state

        params['parameters'] = params.get('parameters', {})
        params['parameters']['title'] = params['parameters'].get('title', cls.DEFAULT_TITLE)

        questionnaire = ConsultationQuestionnaire(patient_id=user.profile.id)
        for field, value in params['questionnaire'].iteritems():
            setattr(questionnaire, field, value)
        db.session.add(questionnaire)
        db.session.commit()

        consultation = ConsultationQueue(user_id=user.id, questionnaire_id=questionnaire.id, require_pediatrician=False,
                                         type='call')
        for field, value in params['consultation'].iteritems():
            setattr(consultation, field, value)
        db.session.add(consultation)
        db.session.commit()

        params['parameters']['consultation_id'] = consultation.id
        return super(TelemedJourney, cls).make_fake_journey(user, params, state=state)

    def _process_state_cancelled(self):
        from healthjoy.telemedicine.utils import process_user_cancel_request
        cid = self.consultation.id
        process_user_cancel_request(self.consultation)
        telemed_client.cancel_consultation(cid)
        logger.info("Canceled consultation {} on the telemed service".format(cid))

        return super(TelemedJourney, self)._process_state_cancelled()

    def _process_state_followup(self):
        if self.consultation.is_behavioral:
            self.state = self.STOP_STATE
            db.session.commit()
        else:
            self._start_followup()
            self._delegate_to_chatbot(self._get_delivery_fire_command(), safe=False)

    def handle_card_read(self):
        from healthjoy.icr.jobs import add_journey_to_delivery

        super(TelemedJourney, self).handle_card_read()
        add_journey_to_delivery.delay(self.id, self.STATES.FOLLOWUP, should_resurrect=True)


class ChatbotChildSimpleEmailJourney(ChatbotChildSimpleCardJourney):
    event_name = None

    def __init__(self, *args, **kwargs):
        notification_args = kwargs.pop('aux_args', [])
        super(ChatbotChildSimpleEmailJourney, self).__init__(*args, **kwargs)
        self._notification_args = notification_args or self.env.get('notification_args', [])

    @property
    def should_send_email(self):
        return True

    def start(self):
        self.set_env('notification_args', self._notification_args)
        notification_attrs = self.get_notification_attrs(*self._notification_args)
        super(ChatbotChildSimpleEmailJourney, self).start()
        db.session.flush()
        if self.should_send_email:
            send_notification.delay(self.user.id, self.event_name, notification_attrs)

    def get_notification_attrs(self, *args):
        return {}


class MedicalBillJourney(ChatbotChildSimpleEmailJourney):
    event_name = 'bill_appointment'
    journey_type = view_type = 'medical_bill'
    DEFAULT_TITLE = 'Medical Bill Review'
    STATES = enum('CARD_STATES', **dict(JOURNEY_CARD_STATES, APPOINTMENT='appointment'))
    COMPLETED_STATES = ChatbotChildSimpleCardJourney.COMPLETED_STATES + (STATES.APPOINTMENT,)
    crm_category = 'Medical Bill Review'
    crm_aux_args = [
        dict(name='Phone', required=True),
        dict(name='Push Text', default=lazy_gettext('Appointment reminder'), required=False)
    ]
    aux_changed = False

    @classmethod
    def add_appointment_date_select(cls):
        if cls.aux_changed:
            cls.crm_aux_args = cls.crm_aux_args[:-1]

        cls.aux_changed = True

        slots_manager = SlotsManager()
        dates = slots_manager.get_empty_slots_dates()
        slots = slots_manager.get_empty_slots(*dates)

        cls.crm_aux_args.append(dict(
            name='Appointment Date Time',
            required=False,
            type_='select',
            choices=[(d.isoformat(), d.strftime("%A %B %d, %Y, %I:%M %p %Z")) for d in slots]
        ))

    use_decision_center = True
    decision_card_type = 'mbr'
    decision_card_cls = MedicalBillReviewCard
    widget_type = 'mbr_card'

    def _schedule_push_before_appointment(self):
        """
        Schedules push which will be send 10 minutes before appointment.
        """
        push_dt = None if current_app.config.get('DEV_OR_STAGE') else \
            self.utc_appointment_dt - timedelta(minutes=10)
        job = send_mobile_push.delay(self.user.id, self._push_text, category=APS_CATEGORY_NOT_CHAT, eta=push_dt)
        self.set_env('appointment_push_job_id', job.id)
        db.session.flush()

    def _update_otrs_ticket_after_appointment(self, slot):
        """
        Adds new article to OTRS ticket after appointment scheduled.
        :param slot:
            MedicalBillReviewAppointmentSlot instance
        """
        from healthjoy.crm import add_article_to_ticket, reopen_ticket

        ticket_subject = 'MBR Appointment scheduled'
        ticket_text = '{} scheduled an appointment to review the MBR results on {}'.format(
            self.user.profile.full_name,
            slot.start_in_cst_display
        )
        add_article_to_ticket(self._state.ticket_id, self.user, ticket_subject, ticket_text)
        reopen_ticket(self._state.ticket_id)

    def _send_notification(self):
        self.set_env('notification_args', self._notification_args)
        notification_attrs = self.get_notification_attrs(*self._notification_args)
        if self.should_send_email:
            send_notification.delay(self.user.id, self.event_name, notification_attrs)
            self._schedule_push_before_appointment()

    def _generate_appointment_datetime_attrs(self, d, t):
        date_string, tz_string = split_tz_date_string('{} {}'.format(d, t).strip())
        self._appointment_dt = (
            date_string and tz_string and
            dateparser_parse(date_string, settings={'TIMEZONE': tz_string, 'RETURN_AS_TIMEZONE_AWARE': True})
        )
        if self._appointment_dt and self.utc_appointment_dt.replace(tzinfo=None) < datetime.utcnow():
            raise ValueError("Appointment can't be in the past")

        return dict(appointment_date=self._appointment_dt and self._appointment_dt.strftime('%A, %B %d, %Y') or '',
                    appointment_time=self._appointment_dt and self._appointment_dt.strftime('%I:%M %p %Z') or '')

    def get_notification_attrs(self, d=None, t=None, phone=None, push_text=None):
        self._appointment_dt = None
        self._push_text = push_text or gettext('Appointment reminder')

        data = {}
        if d and t:
            data.update(self._generate_appointment_datetime_attrs(d, t))
        if phone:
            data['appointment_phone'] = format_phone_number(phone)

        if self._appointment_dt:
            cal = get_ics_calendar(self.utc_appointment_dt, self.utc_appointment_dt + timedelta(minutes=30),
                                   gettext('Medical Bill Review Appointment'))
            add_customerio_attachment(data, 'invite.ics', cal.to_ical())
        return data

    @chatbot_command_handler('fill_slot_for_existing_mbr', with_callback=True)
    def _fill_slot_for_existing_mbr(self, date_str, time_str, tz, phone):
        date_ = dateparser_parse(date_str)
        time_ = datetime.strptime(time_str, '%I:%M %p').time()

        combine_datetime = datetime.combine(date_, time_)
        combine_datetime = get_tz_datetime(combine_datetime, current_tz_name=tz or 'America/Chicago',
                                           dest_tz_name='UTC')

        formatted_date = date_.strftime('%A %B %d, %Y')
        self._notification_args = [formatted_date, ' '.join([time_str, tz]), phone]

        slot = MedicalBillReviewAppointmentSlot.schedule(combine_datetime)
        slot.child_journey_state_id = self.id
        self._update_otrs_ticket_after_appointment(slot)
        self._send_notification()

        self.state = self.STOP_STATE
        db.session.commit()

    def start(self):
        super(MedicalBillJourney, self).start()
        if self.should_send_email:
            self._schedule_push_before_appointment()

    def _process_state_cancelled(self):
        super(MedicalBillJourney, self)._process_state_cancelled()
        job_id = self.env.get('appointment_push_job_id')
        if job_id:
            cancel_job(job_id)

    def _process_state_appointment(self):
        self.update_thread(self.user.last_chat, silent=True)
        self.activate()
        self._delegate_to_chatbot('[mbr_appointment]', safe=False)

    @property
    def utc_appointment_dt(self):
        if not getattr(self, '_appointment_dt', None):
            return
        return self._appointment_dt.astimezone(pytz.UTC)

    @property
    def should_send_email(self):
        if not hasattr(self, '_appointment_dt'):
            return False
        return bool(self._appointment_dt)


class ArticleJourney(ChatbotChildSimpleJourney):
    STATES = enum(
        'QNA_STATES',
        NEW='new',
        RESOLVED='resolved',  # Answered
        FINISHED='finished'
    )
    INIT_STATE = STATES.NEW
    STOP_STATE = STATES.FINISHED
    widget_type = journey_type = 'qna'
    title = 'Q&A'

    def __init__(self, *args, **kwargs):
        question = kwargs.pop('question', None)
        super(ArticleJourney, self).__init__(*args, **kwargs)
        self._question = question or self.env['question']

    def start(self):
        from healthjoy.crm import medical_question

        super(ArticleJourney, self).start()
        question = self._question
        self.set_env('question', question)
        ticket_id, _ = medical_question(self.user, question)
        self.set_ticket(ticket_id)
        db.session.flush()

    def _fill_card(self, answer, resolved_by=None, resolved_by_name=None):
        self.set_env('answer', answer)
        if resolved_by:
            self.set_env('resolved_by', resolved_by)
        if resolved_by_name:
            self.set_env('resolved_by_name', resolved_by_name)
        db.session.commit()

    def _process_state_resolved(self, answer, resolved_by=None, resolved_by_name=None):
        self._fill_card(answer, resolved_by=resolved_by, resolved_by_name=resolved_by_name)
        self._delegate_to_chatbot('[medical question delivery]', safe=False,
                                  extra_context=dict(qna_answer=answer))
        db.session.commit()


class IntakeJourney(ChatbotChildJourney):
    STATES = enum(
        'INTAKE_STATES',
        NEW='new',
        WAIT_FOR_CLOSE='wait_for_close',
        FINISHED='finished',
    )
    INIT_STATE = STATES.NEW
    STOP_STATE = STATES.FINISHED
    widget_type = journey_type = 'intake'
    title = 'Spitball'

    def __init__(self, *args, **kwargs):
        fire_command = kwargs.pop('fire_command', None)
        messages = kwargs.pop('messages', None)
        campaign_type = kwargs.pop('campaign_type', '')
        extra_kwargs = kwargs.pop('extra_kwargs', {})
        super(IntakeJourney, self).__init__(*args, **kwargs)
        self._fire_command = fire_command or self.env.get('fire_command')
        self._messages = messages or self.env.get('messages')
        self._campaign_type = campaign_type or self.env.get('campaign_type', None)
        self._extra_kwargs = extra_kwargs or self.env.get('extra_kwargs', {})
        # Is Improved Journey Tracking supported?
        self._is_ijt_supported = user_client_supports_improved_journey_tracking(self.user)

    def start(self):
        assert self._fire_command or self._messages
        self.set_env('fire_command', self._fire_command)
        self.set_env('messages', self._messages)
        self.set_env('campaign_type', self._campaign_type)
        self.set_env('extra_kwargs', self._extra_kwargs)
        self.set_env("read", False)
        if self._is_ijt_supported:
            notify_outbound_journey_updated_push.delay(self.user.id)
        super(IntakeJourney, self).start()

    def deliver(self):
        logger.info('IntakeJourney deliver called for %s', self.id)

        if self.user.push_notifications_enabled:
            self._deliver_by_push()
        self._deliver_by_email()

        logger.info('IntakeJourney deliver completed for %s', self.id)

    def _deliver_by_email(self):
        """Deliver journey using EMAIL channel."""
        logger.info("Delivering IntakeJourney: %s to user.id: %d using EMAIL channel", self.id, self.user.id)
        send_notification.delay(
            self.user.id, "broadcast_email",
            data={
                'campaign_name': self._extra_kwargs.get('push_title'),
                'notification_message': self._extra_kwargs.get('push_text')
            },
            check=False)

    def _deliver_by_push(self):
        """Deliver journey using PUSH channel."""
        logger.info("Delivering IntakeJourney: %s to user.id: %d using PUSH channel", self.id, self.user.id)
        spitball_name = self.spitball_name
        push_text = self._extra_kwargs.get('push_text')
        if push_text:
            push_kwargs = {'update_thread': True, 'tpa_name': self.user.tpa_name}
            if self._is_ijt_supported:
                push_title = self._extra_kwargs.get('push_title') or self._messages and self._messages[0].get("text")
                push_kwargs.update(
                    category=PUSH_CATEGORY_OUTBOUND_JOURNEY,
                    title=push_title,
                    id=self.id,
                )
            else:
                push_link = self._extra_kwargs.get('push_link')
                if push_link:
                    push_kwargs.update(category=APS_LINK_CATEGORY, title='', link=push_link)

            try:
                text_formatted = push_text.format(user=self.user)
            except (KeyError, ValueError):
                text_formatted = push_text

            send_mobile_push.delay(self.user.id, text_formatted, **push_kwargs)
        else:
            from healthjoy.assistant.jobs import prepare_delivery
            prepare_delivery.delay(self.thread_id, campaign_type=self._campaign_type)

        if spitball_name:
            analytics.track(self.user, 'spitball_push', {
                'name': spitball_name,
                'dow': calendar.day_name[date.today().weekday()],
                'time': datetime.now().hour,
            })

    @property
    def spitball_name(self):
        # TODO: fix this shame
        # should be defined in this manner because of dir(self) used in __init__
        extra_kwargs = getattr(self, '_extra_kwargs', None) or {}
        return extra_kwargs.get('spitball_name')

    def _process_state_finished(self):
        was_completed = False

        is_read = bool(self.env.get("read"))
        if not is_read:
            self.set_env("read", True)
            db.session.commit()
            if self._is_ijt_supported:
                notify_outbound_journey_updated_push.delay(self.user.id)

        if self._is_ijt_supported:
            self._chat.send_system_message(self.thread, OutboundJourneyStateUpdated(
                title=self.meta['display_name'],
                icon_url=self.meta['icon_url'],
            ).render())

        msg_kwargs = {
            'meta': self.meta
        }

        if self._messages:
            for message in self._messages:
                link = message.get('link')
                text = message['text']
                link_type = message.get('link_type')

                if link_type == 'file_link' and link:
                    path = urlparse.urlparse(link).path
                    file_meta = file_manager_storage.get_file_meta(path)

                    self._chat.send_system_message(
                        self.thread,
                        message="",
                        files=[Attach.from_fm_meta(file_meta['data'])],
                        **msg_kwargs
                    )
                elif link_type == 'web_link':
                    self.say(link, **msg_kwargs)
                elif link:
                    self._send_chat_link(link, 'info', text, **msg_kwargs)
                else:
                    self.say(text, **msg_kwargs)

        if self._fire_command:
            was_completed = True
            self._delegate_to_chatbot(
                self._fire_command,
                safe=False,
                campaign_type=self._campaign_type,
                extra_context=self._extra_kwargs,
                **msg_kwargs
            )

        if self._extra_kwargs.get('communication_record_id'):
            publish_communication_event.delay(COMMUNICATION_OPENED, self._extra_kwargs['communication_record_id'])

        spitball_name = self.spitball_name
        if spitball_name:
            analytics.track(self.user, 'spitball_fired', {
                'name': spitball_name,
                'dow': calendar.day_name[date.today().weekday()],
                'time': datetime.now().hour
            })

        if not was_completed:
            self.state = self.STATES.WAIT_FOR_CLOSE
            with with_user_locale(self.user):
                return self.send_x_control(
                    make_select(
                        self._state,
                        choices=[gettext("Close Message")],
                        type_=SELECT_TYPE_BUTTONS,
                        cls=SimpleChoiceElement,
                    )
                )

    def handle_submit_when_wait_for_close(self, el):
        """Handle submit of the 'Close Message' button."""
        self.say(el, msg_id=el.id)
        self.state = self.STOP_STATE
        self._close_chat()

    def is_employer_broadcast(self):
        return 'communication_record_id' in self._state.parameters['extra_kwargs']

    @property
    def meta(self):
        return {
            'display_name': self.user.campaign.title if self.is_employer_broadcast() else 'HealthJoy',
            'icon_url': COMPANY_ICON_URL_PLACEHOLDER if self.is_employer_broadcast() else HJ_ICON_URL,
        }


class DIYJourney(ChatbotChildSimpleJourney):
    has_diy = True

    def __init__(self, *args, **kwargs):
        card_response = kwargs.pop('card_response', '')
        title = kwargs.pop('title', '')
        super(DIYJourney, self).__init__(*args, **kwargs)
        self._card_response = card_response or self.env.get('card_response')
        self._title = title or self.env.get('title')

    def start(self):
        self._fill_card(self._card_response)
        self.set_env('title', self._title)
        db.session.commit()
        self._create_otrs_ticket(self.ticket_default_subject, self._card_response, ticket_state='resolved')
        super(DIYJourney, self).start()
        self.state = self.STATES.RESOLVED


class DIYProviderJourney(DIYJourney):
    journey_type = 'provider_diy'
    view_type = 'note'


class DIYUrgentCareJourney(DIYJourney):
    journey_type = 'urgent_care_diy'
    view_type = 'facility'


class DIYRxSavingsJourney(DIYJourney):
    journey_type = 'rx_savings_diy'
    view_type = 'money'


class DIYTelemedJourney(TelemedJourney):
    journey_type = 'telemed_diy'
    view_type = 'consult'
    has_diy = True

    def __init__(self, thread=None, *args, **kwargs):
        user = thread.assistant_profile.user
        params = next((p for p in CAMPAIGN_FAKE_JOURNEYS.get(user.campaign.alias, [])
                       if p.get('controller') == TelemedJourney.__name__), None) or DEFAULT_TELEMED_FAKE_JOURNEY
        state = self.make_fake_journey(user, params)
        super(DIYTelemedJourney, self).__init__(*args, thread=thread, state=state, **kwargs)
