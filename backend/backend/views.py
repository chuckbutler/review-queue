import re
import base64
import sqlalchemy
import logging

from dateutil import parser

from datetime import (
    date,
    datetime,
    timedelta,
)

from pyramid.httpexceptions import (
    HTTPFound,
    HTTPNotFound,
    )

from pyramid.view import view_config

from .models import (
    DBSession,
    Review,
    Profile,
    User,
    ReviewVote,
)

from .helpers import (
    UserSerializer,
    ReviewSerializer,
    ReviewedSerializer,
    get_lp,
)

from tasks import create_user


log = logging.getLogger(__name__)


@view_config(route_name='home', renderer='templates/dashboard.pt')
def dashboard(request):
    #reviews = DBSession.query(Review).group_by(Review.review_category_id).all()
    reviews = DBSession.query(Review).filter(Review.state != 'REVIEWED',
                                             Review.state != 'NEW',
                                             Review.state != 'IN PROGRESS',
                                             Review.state != 'CLOSED',
                                             Review.state != 'MERGED',
                                             Review.state != 'ABANDONDED').order_by(Review.updated).all()
    incoming = DBSession.query(Review).filter_by(state='NEW').order_by(Review.updated).all()
    return dict(reviews=reviews, incoming=incoming)


@view_config(route_name='find_user', renderer='templates/user.pt')
def find_user(req):
    username = req.params.get('user')
    lpuser = req.cookies.get('lpuser')

    if not lpuser and username:
        profile = DBSession.query(Profile).filter_by(username=username).first()
        if not profile:
            return dict(error='No profile found')
        lpuser = username

    if not lpuser:
        return dict(user=dict(), reviews=dict(), submitted=dict(), me=True)

    req.matchdict['username'] = lpuser
    return user(req)


@view_config(route_name='search_user', renderer='json')
def search_user(req):
    query = req.params['q']
    matches = DBSession.query(User).filter(User.name.like('%%%s%%' % query)).all()
    return UserSerializer(matches, many=True, exclude=('reviews', )).data


@view_config(route_name='query_results', renderer='templates/search.pt')
def saved_search(request):
    q = request.matchdict['filter']

    #return search(request, q)


@view_config(route_name='login', renderer='json')
def login(req):
    mode = req.params.get('openid.mode')
    log.debug('mode: %s' % mode)

    if mode == 'cancel':
        log.debug('User canceled. Why? Who cares')
        return HTTPFound(location=req.route_url('home'))

    if mode == 'id_res':
        print(req.params)
        claimed = req.params.get('openid.claimed_id')
        username = req.params.get('openid.sreg.nickname')

        profile = DBSession.query(Profile).filter_by(claimed=claimed).first()

        if not profile:
            log.debug('Never logged in before? Welcome.')
            # So, first time login. Try to match username?
            profile = DBSession.query(Profile).filter_by(username=username).first()

        if profile:
            user = profile.user

        if not profile:
            log.debug('Fuck off, you havent done jack shit')
            # Okay, so we still don't have a profile. wtf guys. GET YOUR REVIEW ON. Create a user for now? Sure.
            lp = get_lp()
            person = lp.load('https://api.launchpad.net/1.0/~%s' % username)
            user = create_user(person)
            profile = user.profile[0]

        if not profile.claimed:
            profile.claimed = claimed

    req.session['user'] = user.id
    return HTTPFound(location=req.route_url('home'))

@view_config(route_name='query', renderer='templates/search.pt')
def serach(request):
    filters = {'reviewer': [],
               'owner': [],
               'from': None,
               'to': None,
               'source': None,
               'state': [],
              }
    if not request.params:
        week_ago = datetime.now() - timedelta(days=7)
        filters['from'] = week_ago.strftime("%Y-%m-%d %H:%M")
        return dict(results=None, filters=filters)

    for f in filters:
        if f in request.params:
            val = request.params[f]
            if ',' in val:
                val = val.split(',')
            filters[f] = val

    q = DBSession.query(Review)

    if filters['owner']:
        if not isinstance(filters['owner'], list):
            filters['owner'] = [filters['owner']]
        q = q.filter(Review.user_id.in_(filters['owner']))
    if filters['state']:
        if not isinstance(filters['state'], list):
            filters['state'] = [filters['state']]
        q = q.filter(Review.state.in_(filters['state']))
    if filters['from']:
        td = parser.parse(filters['from'])
        q = q.filter(Review.updated >= td)
    if filters['to']:
        td = parser.parse(filters['to'])
        q = q.filter(Review.updated <= td)
    if filters['reviewer']:
        if not isinstance(filters['reviewer'], list):
            filters['reviewer'] = [filters['reviewer']]
        q = (q.join(ReviewVote.review)
              .filter(ReviewVote.user_id.in_(filters['reviewer'])))

    data = q.all()
    return dict(results=data, filters=filters)


@view_config(route_name='lock_review', renderer='json')
def lock_review(request):
    if 'User' not in request.session:
        return dict(error='Not logged in')

    review_id = request.matchdict['review']
    review = DBSession.query(Review).get(review_id)
    user = request.session['User']

    if not review:
        return dict(error='Unable to find review %s' % review_id)

    if review.locked:
        return dict(error='Review already locked by: %s' % review.locker.name)

    review.lock(user)
    return dict(error=None)


@view_config(route_name='show_review', renderer='templates/show_review.pt')
@view_config(route_name='show_reviews', renderer='json')
def review(req):
    review_id = req.matchdict['review']
    review = DBSession.query(Review).filter_by(id=review_id).first()

    if not review:
        return HTTPNotFound('No such review')

    return dict((col.name, str(getattr(review, col.name))) for col in sqlalchemy.orm.class_mapper(review.__class__).mapped_table.c)


@view_config(route_name='id_user', accept="application/json", renderer='json')
def id_json(request):
    data = DBSession.query(User).get(request.matchdict['id'])

    return UserSerializer(data, exclude=('reviews', )).data


@view_config(route_name='view_user', accept="application/json", renderer='json')
def user_json(request):
    data = user(request)

    return dict(user=UserSerializer(data['user'], exclude=('reviews', )).data,
                reviews=ReviewedSerializer(data['reviews'], many=True).data,
                submitted=ReviewSerializer(data['submitted'], exclude=('owner', ), many=True).data)


@view_config(route_name='view_user', accept="text/html", renderer='templates/user.pt')
def user(request):
    username = request.matchdict['username']
    user = DBSession.query(Profile).filter_by(username=username).first().user
    if not user:
        return HTTPNotFound('No such user')
    submitted = DBSession.query(Review).filter_by(owner=user).order_by(Review.updated).all()
    reviews = (DBSession.query(ReviewVote)
                        .filter_by(owner=user)
                        .join(ReviewVote.review)
                        .filter(Review.owner != user)
                        .group_by(Review).order_by(Review.updated)).all()

    return dict(user=user, reviews=reviews, submitted=submitted)
