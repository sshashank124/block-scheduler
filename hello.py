import os
import jinja2
import webapp2
import sys
import re

sys.path.insert(0, 'libs')

from apiclient import discovery, errors
from datetime import datetime
from copy import deepcopy
from random import randint

from oauth2client.appengine import OAuth2Decorator
from google.appengine.api import users, mail
from google.appengine.ext import db

with open('secrets', 'r') as f:
    c_id, c_secret = f.read().split('\n')

decorator = OAuth2Decorator(
  client_id=c_id,
  client_secret=c_secret,
  scope='https://www.googleapis.com/auth/calendar',
  access_type='offline',
  approval_prompt='force')

service = discovery.build('calendar', 'v3')

template_dir = os.path.join(os.path.dirname(__file__), 'templates')
jinja_env = jinja2.Environment(loader = jinja2.FileSystemLoader(template_dir),
                               autoescape = True)

#list of campuses' calendar id
campuses = {
 'iskl_ms': ["iskl.edu.my_7fqt0f2sj8odprhnalgdsa1a5k@group.calendar.google.com", "08:00"],
 'iskl_es': ["iskl.edu.my_o4tp15b8o4b9iaj1t69b93s62o@group.calendar.google.com", "08:00"]}

#list of form element names
items = ('name',
         'description',
         'attendees',
         'date_start',
         'date_end',
         'time_start',
         'time_end',
         'campus',
         'day')

def human_date(d):
    try:
        d2 = datetime.strptime(d, "%Y-%m-%d")
        return d2.strftime("%b %d %Y")
    except ValueError:
        return d

def human_time(d):
    try:
        d2 = datetime.strptime(d, "%H:%M")
        return d2.strftime("%I:%M %p")
    except ValueError:
        return d

#helper class with convenience methods
class Handler(webapp2.RequestHandler):
    
    def write(self, *a, **kw):
        self.response.out.write(*a, **kw)

    def render_str(self, template, **params):
        t = jinja_env.get_template(template)
        return t.render(params)

    def render(self, template, **kw):
        self.write(self.render_str(template, **kw))

    def element(self, element):
        a = self.request.get_all(element)
        if len(a) == 1:
            return a[0]
        else:
            return a

class User(db.Model):
    user_email = db.StringProperty(required = True)
    last_used = db.DateTimeProperty(required = True, auto_now = True)
    views = db.IntegerProperty(required = True, default = 1)
    events_created = db.IntegerProperty(required = True, default = 0)
    events_deleted = db.IntegerProperty(default = 0)

def update_user(email_id, events_c, events_d):
    user_db = User.gql("WHERE user_email = :1", email_id).fetch(limit=1)
    
    #if user already exists, increment the number of visits and events created
    #otherwise, add the new user
    if user_db:
        user_db = user_db[0]
        user_db.views += 1
        user_db.events_created += events_c
        user_db.events_deleted += events_d
        user_db.put()
    else:
        u = User(user_email=email_id, events_created=events_c, events_deleted=events_d)
        u.put()

class CreatePage(Handler):
    page = "Create Event"
    @decorator.oauth_required
    def get(self):
        user = users.get_current_user().email()
        update_user(user, 0, 0)
        total = '{:,}'.format(sum(u.events_created for u in db.GqlQuery("SELECT events_created FROM User")))
        self.render("scheduler.html", user=user, total_events=total, page=self.page)

    @decorator.oauth_aware
    def post(self):
        #collect form input values
        inputs = dict((i, self.element(i)) for i in items)
        a = deepcopy(inputs)
        del a['attendees']

        cal_ids = self.request.get('cal_ids').split('\n')

        attendees = inputs['attendees']
        emails = attendees.split('\n')

        inputs['attendees'] = [{'email':e.strip()} for e in emails if e]

        attend_re = re.compile(r'.*?@.*')

        #verify input data and generate appropriate error message
        error_message = False
        if any(not i for i in a.values()):
            error_message = "Please fill all fields (Attendees: Optional)"
        elif datetime.strptime(inputs['date_end'], "%Y-%m-%d") <= datetime.strptime(inputs['date_start'], "%Y-%m-%d"):
            error_message = "End Date should be after Start Date"
        elif datetime.strptime(inputs['time_end'], "%H:%M") <= datetime.strptime(inputs['time_start'], "%H:%M"):
            error_message = "End Time should be after Start Time"
        elif attendees and any(attend_re.match(a) is None for a in emails):
            error_message = "Attendees' emails not in correct format<br>Refer to <a href=\"/help\" style=\"color: #FFFF00\">Help</a> for usage"

        del a['day']

        if error_message:
            total = '{:,}'.format(sum(u.events_created for u in db.GqlQuery("SELECT events_created FROM User")))
            #re-render form with error message
            self.render("scheduler.html",
                        error=error_message,
                        page=self.page,
                        user=users.get_current_user().email(),
                        total_events=total,
                        attendees=attendees,
                        **a)
        else:
            #authorize http requester
            http = decorator.http()

            del a['description']

            #get list of events from school calendar
            events = service.events().list(calendarId=campuses[inputs['campus']][0],
                                           singleEvents=True,
                                           timeMin=inputs['date_start']+'T00:00:00+00',
                                           timeMax=inputs['date_end']+'T00:00:00+00').execute(http=http)
            
            num_events = 0
            ids = []
            email_attendees_once = False
            for event in events.get('items'):
                #check if the day is a school day and in one of the selected days from the form
                if event.get('summary')[4:] in inputs.get('day'):
                    #create appropriate format for start and end time of the event
                    timezone = campuses[inputs['campus']][1]
                    starttime = event['start']['date']+'T'+inputs['time_start']+":00+"+timezone
                    endtime = event['start']['date']+'T'+inputs['time_end']+":00+"+timezone

                    #JSON representation of the event
                    event = {
                        'summary': inputs['name'],
                        'description': inputs['description'],
                        'start': {
                            'dateTime': starttime
                        },
                        'end': {
                            'dateTime': endtime
                        }
                    }

                    if not email_attendees_once:
                        event['attendees'] = inputs['attendees']
                        email_attendees_once = True

                    #insert event into primary calendar
                    ids.append(service.events().insert(calendarId='primary',
                                            body=event, sendNotifications=True).execute(http=http)['id'])

                    for cal in cal_ids:
                        if cal:
                            try:
                                service.events().insert(calendarId=cal,
                                            body=event, sendNotifications=True).execute(http=http)['id']
                            except errors.HttpError:
                                pass

                    num_events += 1

            #redirect to success page with GET parameters of the event
            self.redirect('/success?'+'&'.join(i+'='+j for i,j in a.items())
                         +'&'+'&'.join('day='+i for i in inputs['day'])
                         +'&num_events='+str(num_events)
                         +'&event_id='+','.join(ids))

class SuccessPage(Handler):
    page = "Calendar Updated"

    def get(self):
        #get events from the GET request parameters in the URL
        params = dict((i, self.element(i)) for i in items)
        params['num_events'] = self.element('num_events')

        #create human-readable dates
        params['date_start'] = human_date(params['date_start'])
        params['date_end'] = human_date(params['date_end'])
        params['time_start'] = human_time(params['time_start'])
        params['time_end'] = human_time(params['time_end'])

        #create a sentence list of days opted for
        params['day'] = ', '.join(params['day'])

        #get logged in user and if no user is logged in, redirect to main page
        user = ''
        try:
            user = users.get_current_user().email()
        except AttributeError:
            self.redirect('/')
            return

        #add user's email to template parameters for calendar displaying
        params['user'] = user

        #update user's visits and events created
        update_user(user, int(params['num_events']), 0)

        #render out the event creation result and details
        self.render("success.html", page=self.page, added=True, **params)

    @decorator.oauth_aware
    def post(self):
        user = users.get_current_user().email()
        http = decorator.http()

        event = self.request.get('event_id').split(',')

        num_events = 0
        for i in event:
            try:
                service.events().delete(calendarId='primary', eventId=i).execute(http=http)
                num_events += 1
            except errors.HttpError:
                pass

        update_user(user, 0, num_events)

        self.render("success.html",
                    page=self.page,
                    num=num_events,
                    user=user,
                    added=False)

class DeletePage(Handler):
    page = "Delete Events"
    def get(self):
        user = users.get_current_user().email()
        update_user(user, 0, 0)
        self.render("delete.html", page=self.page, user=user)

    @decorator.oauth_aware
    def post(self):
        user = users.get_current_user().email()

        #collect form input values
        event_name = self.element('name')

        #ensure that the fields are satisfactorily filled
        if not event_name:
            self.render("delete.html",
                        error="Please input event name",
                        page=self.page,
                        user=user)
        else:
            #authorize http requester
            self.redirect('/delete/confirmation?event_name='+event_name)

class DeleteConfirmationPage(Handler):
    page = "Delete Confirmation"

    @decorator.oauth_aware
    def get(self):
        event_name = self.element('event_name')

        #authorize http request
        http = decorator.http()

        #get list of events from personal calendar
        events = service.events().list(calendarId='primary',
                                       singleEvents=True,
                                       timeMin='2014-08-01T00:00:00+00:00',
                                       maxResults=2000).execute(http=http).get('items')

        num_events = 0
        if events:
            for event in events:
                if event.get('summary') == event_name:
                    num_events += 1

        self.render('confirmation.html', num_events=num_events, page=self.page)

    @decorator.oauth_aware
    def post(self):
        if self.element('delete') == 'Yes':
            user = users.get_current_user().email()
            event_name = self.element('event_name')

            #authorize http request
            http = decorator.http()

            #get list of events from personal calendar
            events = service.events().list(calendarId='primary',
                                           singleEvents=True,
                                           timeMin='2014-08-01T00:00:00+00:00',
                                           maxResults=2000).execute(http=http).get('items')

            #delete specified events
            num_events = 0
            if events:
                for event in events:
                    if event.get('summary') == event_name:
                        try:
                            service.events().delete(calendarId='primary',
                                                    eventId=event.get('id')).execute(http=http)
                            num_events += 1
                        except errors.HttpError:
                            pass

            update_user(user, 0, num_events)

            self.render("success.html",
                        page="Calendar Updated",
                        num=num_events,
                        user=user,
                        added=False)
        else:
            self.redirect('/delete')

class FeedbackPage(Handler):
    page = "Feedback"
    def get(self):
        update_user(users.get_current_user().email(), 0, 0)
        self.render("feedback.html", page=self.page, submitted=False)

    def post(self):
        body = self.element("feedback")
        error = ""
        if not body:
            error = "Please enter some text"
            self.render("feedback.html", page=self.page, error=error, submitted=False)
        else:
            mail.send_mail(sender=users.get_current_user().email(),
                           to="sshashank124@gmail.com",
                           subject="Block-Scheduler User Feedback",
                           body=body)
            self.render("feedback.html", page=self.page, submitted=True)

class SecretPage(Handler):
    page = "Secret"
    def get(self):
        self.render('message.html',
                    page=self.page,
                    message="Congratulations! You found the secret page! Don't tell ANYONE!")

class NoPage(Handler):
    page = "Page Not Found"
    def get(self):
        n1 = randint(0,400)
        m = "Error {0}+{1}: This is not the page you are looking for.".format(n1, 404-n1)
        self.render('message.html',
                    page=self.page,
                    message=m)

class HelpPage(Handler):
    page = "Help"
    def get(self):
        update_user(users.get_current_user().email(), 0, 0)
        self.render('help.html', page=self.page)

app = webapp2.WSGIApplication([('/', CreatePage),
                               ('/success', SuccessPage),
                               ('/delete', DeletePage),
                               ('/delete/confirmation', DeleteConfirmationPage),
                               ('/feedback', FeedbackPage),
                               ('/secret', SecretPage),
                               ('/help', HelpPage),
                               (decorator.callback_path, decorator.callback_handler()),
                               ('/.*', NoPage)],
                               debug=True)
