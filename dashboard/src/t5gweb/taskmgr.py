"""start celery and manage tasks"""
import logging
import time
import datetime
import os
import json
import t5gweb.libtelco5g as libtelco5g
import t5gweb.cache as cache
import t5gweb.t5gweb as t5gweb
from celery import Celery
from celery.schedules import crontab
import bugzilla
import redis
from t5gweb.utils import (
    email_notify,
    slack_notify,
    set_cfg
)

mgr = Celery('t5gweb', broker='redis://redis:6379/0', backend='redis://redis:6379/0')

#https://docs.celeryproject.org/en/stable/userguide/periodic-tasks.html#entries
@mgr.on_after_configure.connect
def setup_scheduled_tasks(sender, **kwargs):

    cfg = set_cfg()

    # check for new cases
    sender.add_periodic_task(
        crontab(hour='*', minute='10'), # 10 mins after every hour
        portal_jira_sync.s(),
        name='portal2jira_sync',
    )

    # ensure case severities match card priorities
    sender.add_periodic_task(
        crontab(hour='3', minute='12'), # everyday at 3:12
        t_sync_priority.s(),
        name='priority_sync',
    )

    # update card cache
    sender.add_periodic_task(
        crontab(hour='*', minute='21'), # on the hour + offset
        cache_data.s('cards'),
        name='card_sync',
    )

    # update case cache
    sender.add_periodic_task(
        crontab(hour='*', minute='*/15'), # every 15 minutes
        cache_data.s('cases'),
        name='case_sync',
    )

    # update details cache
    sender.add_periodic_task(
        crontab(hour='*/12', minute='24'), # twice a day
        cache_data.s('details'),
        name='details_sync',
    )

    # update Jira bug details cache
    sender.add_periodic_task(
        crontab(hour='*/12', minute='54'), # twice a day
        cache_data.s('issues'),
        name='issues_sync',
    )

    # generate daily stats
    sender.add_periodic_task(
        crontab(hour='4', minute='40'), # every day at 4:40
        cache_stats.s(),
        name='cache_stats',
    )


    ## optional tasks

    # update bugzilla details cache
    if cfg['bz_key'] is not None and cfg['bz_key'] != '':
        sender.add_periodic_task(
            crontab(hour='*/12', minute='48'), # twice a day
            cache_data.s('bugs'),
            name='bugs_sync',
        )

    # update escalations cache
    if cfg['smartsheet_access_token'] or (cfg['jira_escalations_project'] and cfg['jira_escalations_label']):
        sender.add_periodic_task(
            crontab(hour='*/6', minute='37'), # 4x a day
            cache_data.s('escalations'),
            name='escalations_sync',
        )

    # tag telco5g bugzillas and JIRAs with 'Telco' and/or 'Telco:Case'
    if 'telco5g' in cfg['query']:
        sender.add_periodic_task(
            crontab(hour='*/24', minute='33'), # once a day + 33 for randomness
            tag_bz.s(),
            name='tag_bz',
        )
    
    # update watchlist cache
    if cfg['watchlist_url'] is not None and cfg['watchlist_url'] != '':
        sender.add_periodic_task(
            crontab(hour='*/12', minute='7'), # twice a day + 7 mins
            cache_data.s('watchlist'),
            name='watchlist_sync',
        )

@mgr.task
def portal_jira_sync():
    
    logging.warning("job: checking for new cases")
    cfg = set_cfg()
    max_to_create = os.environ.get('max_to_create')

    start = time.time()
    
    cases = libtelco5g.redis_get('cases')
    cards = libtelco5g.redis_get('cards')
    
    open_cases = [case for case in cases if cases[case]['status'] != 'Closed']
    card_cases = [cards[card]['case_number'] for card in cards]
    logging.warning("found {} cases in JIRA".format(len(card_cases)))
    new_cases = [case for case in open_cases if case not in card_cases]
    logging.warning("new cases: {}".format(new_cases))

    if len(new_cases) > int(max_to_create):
        logging.warning("Warning: more than {} cases ({}) will be created, so refusing to proceed. Please check log output\n".format(max_to_create, len(new_cases)))
        email_content = ['Warning: more than {} cases ({}) will be created, so refusing to proceed. Please check log output\n"'.format(max_to_create, len(new_cases))]
        email_content += ['New cases: {}\n"'.format(new_cases)]
        cfg['to'] = os.environ.get('alert_email')
        cfg['subject'] = 'High New Case Count Detected'
        email_notify(cfg, email_content)
    elif len(new_cases) > 0:
        logging.warning("need to create {} cases".format(len(new_cases)))
        message_content, new_cards = libtelco5g.create_cards(cfg, new_cases, action='create')
        if message_content:
            logging.warning("notifying team about new JIRA cards")
            email_notify(cfg, message_content)
            if cfg['slack_token'] and cfg['slack_channel']:
                slack_notify(cfg, message_content)
            else:
                logging.warning("no slack token or channel specified")
            cards.update(new_cards)
            libtelco5g.redis_set('cards', json.dumps(cards))
    else:
        logging.warning("no new cards required")
            
    end = time.time()
    logging.warning("synced to jira in {} seconds".format(end - start))

@mgr.task(autoretry_for=(Exception,), max_retries=5, retry_backoff=30)
def cache_data(data_type):
    
    logging.warning("job: sync {}".format(data_type))

    cfg = set_cfg()

    if data_type == 'cases':
        cache.get_cases(cfg)
    elif data_type == 'cards':
        # Use redis locks to prevent concurrent refreshes

        have_lock = False
        refresh_lock = redis.Redis(host='redis').lock("refresh_lock", timeout=60*5)
        try:
            have_lock = refresh_lock.acquire(blocking=False)
            if have_lock:
               cache.get_cards(cfg)
        finally:
            if have_lock:
                refresh_lock.release()
    elif data_type == 'details':
        cache.get_case_details(cfg)
    elif data_type == 'bugs':
        cache.get_bz_details(cfg)
    elif data_type == 'issues':
        cache.get_issue_details(cfg)
    elif data_type == 'escalations':
        cache.get_escalations(cfg)
    elif data_type == 'watchlist':
        cache.get_watchlist(cfg)
    else:
        logging.warning("unknown data type")

@mgr.task(autoretry_for=(Exception,), max_retries=3, retry_backoff=30)
def tag_bz():
    ## telco5g specific

    cfg = set_cfg()
    if cfg['jira_query'] != 'field':
        logging.warning("bz tagging not enabled for {}".format(cfg['jira_query']))
        return

    logging.warning("getting bugzillas")
    bz_url = "bugzilla.redhat.com"
    bz_api = bugzilla.Bugzilla(bz_url, api_key=cfg['bz_key'])
    cases = libtelco5g.redis_get("cases")
    bugs = libtelco5g.redis_get('bugs')
    issues = libtelco5g.redis_get('issues')
    jira_conn = libtelco5g.jira_connection(cfg)

    logging.warning("tagging bugzillas")
    for case in bugs:
        if case in cases:
            for bug in bugs[case]:
                try:
                    bz = bz_api.getbug(bug['bugzillaNumber'])
                except:
                    logging.warning("error: {} is restricted".format(bug['bugzillaNumber']))
                    bz = None
                if bz:
                    update = None
                    if "telco" not in bz.internal_whiteboard.lower():
                        update = bz_api.build_update(internal_whiteboard="Telco Telco:Case " + bz.internal_whiteboard, minor_update=True)
                    elif "telco:case" not in bz.internal_whiteboard.lower():
                        update = bz_api.build_update(internal_whiteboard=bz.internal_whiteboard + " Telco:Case", minor_update=True)
                    if update:
                        logging.warning("tagging BZ:" + str(bz.id))
                        try:
                            bz_api.update_bugs([bz.id], update)
                        except:
                            logging.warning("Tried and failed to tag " + str(bz.id))
                            continue
    logging.warning("tagging Jira Bugs")
    for case in issues:
        if case in cases:
            for issue in issues[case]:
                try:
                    card = jira_conn.issue(issue['id'])
                    internal_whiteboard = card.fields.customfield_12322040
                except AttributeError:
                    logging.warning("No Internal Whiteboard field for {}, skipping".format(jira_conn.issue(issue['id'])))
                    continue
                if internal_whiteboard is None:
                    internal_whiteboard = ""
                if "telco" not in internal_whiteboard.lower():
                    logging.warning("tagging Jira Bug:" + str(card))
                    internal_whiteboard = "Telco Telco:Case " + internal_whiteboard
                    update = card.update(customfield_12322040=internal_whiteboard)
                elif "telco:case" not in internal_whiteboard.lower():
                    logging.warning("tagging Jira Bug:" + str(card))
                    internal_whiteboard = internal_whiteboard + " Telco:Case"
                    update = card.update(customfield_12322040=internal_whiteboard)

@mgr.task
def cache_stats():
    logging.warning("job: cache stats")
    cache.get_stats()

@mgr.task(bind=True)
def refresh_background(self):
    '''Refresh Jira cards cache in background. If the refresh is already in progress, the task will be locked and won't run.
    The lock is released when the task completes or after five minutes.
    Lock code derived from http://loose-bits.com/2010/10/distributed-task-locking-in-celery.html
    '''

    have_lock = False
    refresh_lock = redis.Redis(host='redis').lock("refresh_lock", timeout=60*5)
    try:
        have_lock = refresh_lock.acquire(blocking=False)
        if have_lock:
            libtelco5g.redis_set('refresh_id', json.dumps(self.request.id))
            cfg = set_cfg()
            cache.get_cards(cfg, self, background=True)
            response = {'current': 100, 'total': 100, 'status': 'Done', 'result': 'Refresh Complete'}
        else:
            response = {'locked': 'Task is Locked'}
    finally:
        if have_lock:
            refresh_lock.release()
    return response

@mgr.task
def t_sync_priority():
    '''Ensure that the severity of a case matches the priority of the card'''
    logging.warning("sync case severity to card priority...")
    cfg = set_cfg()
    libtelco5g.sync_priority(cfg)
    logging.warning("...sync completed")
