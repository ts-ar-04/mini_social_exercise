from flask import Flask, render_template, request, redirect, url_for, session, flash, g, abort
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet
import collections
import json
import math
import sqlite3
import hashlib
import re
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = '123456789' 
DATABASE = 'database.sqlite'

# Load censorship data
# WARNING! The censorship.dat file contains disturbing language when decrypted. 
# If you want to test whether moderation works, 
# you can trigger censorship using these words: 
# tier1badword, tier2badword, tier3badword
ENCRYPTED_FILE_PATH = 'censorship.dat'
fernet = Fernet('xpplx11wZUibz0E8tV8Z9mf-wwggzSrc21uQ17Qq2gg=')
with open(ENCRYPTED_FILE_PATH, 'rb') as encrypted_file:
    encrypted_data = encrypted_file.read()
decrypted_data = fernet.decrypt(encrypted_data)
MODERATION_CONFIG = json.loads(decrypted_data)
TIER1_WORDS = MODERATION_CONFIG['categories']['tier1_severe_violations']['words']
TIER2_PHRASES = MODERATION_CONFIG['categories']['tier2_spam_scams']['phrases']
TIER3_WORDS = MODERATION_CONFIG['categories']['tier3_mild_profanity']['words']

def get_db():
    """
    Connect to the application's configured database. The connection
    is unique for each request and will be reused if this is called
    again.
    """
    if 'db' not in g:
        g.db = sqlite3.connect(
            DATABASE,
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = sqlite3.Row

    return g.db


@app.teardown_appcontext
def close_connection(exception):
    """Closes the database again at the end of the request."""
    db = g.pop('db', None)

    if db is not None:
        db.close()


def query_db(query, args=(), one=False, commit=False):
    """
    Queries the database and returns a list of dictionaries, a single
    dictionary, or None. Also handles write operations.
    """
    db = get_db()
    
    # Using 'with' on a connection object implicitly handles transactions.
    # The 'with' statement will automatically commit if successful, 
    # or rollback if an exception occurs. This is safer.
    try:
        with db:
            cur = db.execute(query, args)
        
        # For SELECT statements, fetch the results after the transaction block
        if not commit:
            rv = cur.fetchall()
            return (rv[0] if rv else None) if one else rv
        
        # For write operations, we might want the cursor to get info like lastrowid
        return cur

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None

@app.template_filter('datetimeformat')
def datetimeformat(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    else:
        return "N/A"
    return dt.strftime('%b %d, %Y %H:%M')

REACTION_EMOJIS = {
    'like': '❤️', 'love': '😍', 'laugh': '😂',
    'wow': '😮', 'sad': '😢', 'angry': '😠',
}
REACTION_TYPES = list(REACTION_EMOJIS.keys())


# =====================================================================
# CODING ASSIGNMENT 1: Contribution and Commitment
#
# Feature 1 - "Needs a reply" feed       -> Ch.2 Design Claim 1
# Feature 2 - Weekly contribution goals  -> Ch.2 Design Claims 13, 14, 15
# Feature 3 - "Return the favor" panel   -> Ch.3 Design Claim 31
# =====================================================================

def init_assignment1_db():
    """
    Creates the extra table and indexes the Assignment 1 features need.
    Runs once when the app starts. 'IF NOT EXISTS' makes it safe to run
    every time, so the existing database never has to be edited by hand.
    """
    db = sqlite3.connect(DATABASE)
    # Feature 2: one row per user holding their current weekly goal.
    db.execute('''
        CREATE TABLE IF NOT EXISTS user_goals (
            user_id       INTEGER PRIMARY KEY,
            posts_goal    INTEGER NOT NULL,
            comments_goal INTEGER NOT NULL,
            updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
        )
    ''')
    # Features 1 and 3 run many "does this post have comments/reactions"
    # lookups. Without these indexes every lookup scans the whole table.
    db.execute('CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_comments_user ON comments(user_id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_reactions_post ON reactions(post_id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_reactions_user ON reactions(user_id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_posts_user ON posts(user_id)')
    db.commit()
    db.close()


init_assignment1_db()


# ---------- Version label (debug helper) ----------
# Bump APP_VERSION whenever the code changes. The label at the top of every
# page shows it together with the time the server started, so it is easy to
# confirm that the browser is really showing the newest code.
APP_VERSION = 'Assignment 2 - v2.0'
SERVER_STARTED = datetime.now().strftime('%H:%M:%S')
print(f' * MiniSocial {APP_VERSION} (server started {SERVER_STARTED})')


@app.context_processor
def inject_version():
    """Makes the version label available in every template (shown in base.html.j2)."""
    return {'app_version': APP_VERSION, 'server_started': SERVER_STARTED}


# ---------- Feature 1: "Needs a reply" (Design Claim 1) ----------

# SQL condition that is true when a post has no comments at all.
# "Needed contribution" on MiniSocial = a reply to a post nobody has answered.
UNANSWERED_CONDITION = "NOT EXISTS (SELECT 1 FROM comments c WHERE c.post_id = p.id)"


@app.context_processor
def inject_unanswered_count():
    """
    Makes 'unanswered_count' available in every template, so the navbar
    can always show how many posts are still waiting for a reply.
    The user's own posts are excluded: you can't answer yourself.
    """
    user_id = session.get('user_id')
    if not user_id:
        return {'unanswered_count': 0}
    row = query_db(
        f"SELECT COUNT(*) AS cnt FROM posts p WHERE {UNANSWERED_CONDITION} AND p.user_id != ?",
        (user_id,), one=True)
    return {'unanswered_count': row['cnt'] if row else 0}


def days_waiting(created_at):
    """Returns how many whole days a post has existed (used for the 'waiting' badge)."""
    if isinstance(created_at, str):
        created_at = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
    if not isinstance(created_at, datetime):
        return 0
    return max(0, (datetime.utcnow() - created_at).days)


# ---------- Feature 2: Weekly goals (Design Claims 13, 14, 15) ----------

# Floors for the system-suggested goal, so new or inactive users still
# get a goal that asks something of them (Claim 13: "highly challenging").
MIN_SUGGESTED_POSTS = 3
MIN_SUGGESTED_COMMENTS = 5
GOAL_CHALLENGE_FACTOR = 1.5   # suggested goal = 150% of the user's usual weekly activity


def current_week_bounds():
    """
    Returns (start, end) of the current goal week as UTC datetimes.
    A week runs Monday 00:00 to the next Monday 00:00. The database stores
    timestamps in UTC (CURRENT_TIMESTAMP), so UTC is used here too.
    """
    now = datetime.utcnow()
    start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=7)


def to_sql_ts(dt):
    """Formats a datetime the same way SQLite stores CURRENT_TIMESTAMP, so string comparison works."""
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def count_activity(user_id, since, until=None):
    """Counts a user's posts and comments created in the time window [since, until)."""
    until = until or datetime.utcnow() + timedelta(days=1)
    args = (user_id, to_sql_ts(since), to_sql_ts(until))
    posts = query_db('SELECT COUNT(*) AS cnt FROM posts WHERE user_id = ? AND created_at >= ? AND created_at < ?',
                     args, one=True)['cnt']
    comments = query_db('SELECT COUNT(*) AS cnt FROM comments WHERE user_id = ? AND created_at >= ? AND created_at < ?',
                        args, one=True)['cnt']
    return posts, comments


def suggest_goal(user_id):
    """
    System-suggested goal (Claim 13 allows "self-set or system-suggested").
    Takes the user's average weekly posts/comments over the last 4 full weeks
    and raises it by 50%, so the goal is specific AND challenging, but still
    based on what this particular user has shown they can do.
    """
    week_start, _ = current_week_bounds()
    four_weeks_ago = week_start - timedelta(weeks=4)
    posts, comments = count_activity(user_id, four_weeks_ago, week_start)
    suggested_posts = max(MIN_SUGGESTED_POSTS, math.ceil(posts / 4 * GOAL_CHALLENGE_FACTOR))
    suggested_comments = max(MIN_SUGGESTED_COMMENTS, math.ceil(comments / 4 * GOAL_CHALLENGE_FACTOR))
    return {'posts': suggested_posts, 'comments': suggested_comments,
            'avg_posts': round(posts / 4, 1), 'avg_comments': round(comments / 4, 1)}


def get_goal_progress(user_id):
    """
    Builds everything the progress widgets need: the goal, this week's
    counts, percentages and time left until the deadline.
    Returns None if the user has not set a goal yet.
    """
    goal = query_db('SELECT posts_goal, comments_goal FROM user_goals WHERE user_id = ?', (user_id,), one=True)
    if not goal:
        return None

    week_start, week_end = current_week_bounds()
    posts_done, comments_done = count_activity(user_id, week_start)

    # Deadline (Claim 14): time remaining until the week closes.
    remaining = week_end - datetime.utcnow()
    days_left = remaining.days
    hours_left = remaining.seconds // 3600

    def pct(done, target):
        return min(100, round(done / target * 100)) if target else 100

    posts_pct = pct(posts_done, goal['posts_goal'])
    comments_pct = pct(comments_done, goal['comments_goal'])
    return {
        'posts_goal': goal['posts_goal'], 'comments_goal': goal['comments_goal'],
        'posts_done': posts_done, 'comments_done': comments_done,
        'posts_pct': posts_pct, 'comments_pct': comments_pct,
        'days_left': days_left, 'hours_left': hours_left,
        # Last 2 days of the week: the widget turns urgent (red/orange).
        'deadline_close': days_left < 2,
        'completed': posts_pct >= 100 and comments_pct >= 100,
    }


def community_comparison(user_id):
    """
    Compares the user's activity this week with every other member who
    was active this week. Returns the community average and the share of
    active members the user is ahead of.
    """
    week_start, _ = current_week_bounds()
    since = to_sql_ts(week_start)
    rows = query_db('''
        SELECT user_id, COUNT(*) AS total FROM (
            SELECT user_id FROM posts    WHERE created_at >= ?
            UNION ALL
            SELECT user_id FROM comments WHERE created_at >= ?
        ) GROUP BY user_id
    ''', (since, since))
    totals = {r['user_id']: r['total'] for r in rows}
    mine = totals.get(user_id, 0)
    others = [t for uid, t in totals.items() if uid != user_id]
    if not others:
        return {'mine': mine, 'active_members': 0, 'avg': 0, 'ahead_pct': None}
    ahead = sum(1 for t in others if mine > t)
    return {'mine': mine, 'active_members': len(others),
            'avg': round(sum(others) / len(others), 1),
            'ahead_pct': round(ahead / len(others) * 100)}


# ---------- Feature 3: "Return the favor" (Design Claim 31) ----------

def get_favors_to_return(user_id, limit=3):
    """
    Finds members who engaged with MY posts (comments + reactions) more often
    than I engaged with THEIR posts, i.e. people I "owe" a favor. For each,
    picks their newest post I haven't commented on or reacted to yet, so the
    feed can show a concrete, one-click way to return the favor.
    """
    # How many times each other user has engaged with my posts.
    received = query_db('''
        SELECT actor_id, SUM(n_comments) AS n_comments, SUM(n_reactions) AS n_reactions
        FROM (
            SELECT c.user_id AS actor_id, COUNT(*) AS n_comments, 0 AS n_reactions
            FROM comments c JOIN posts p ON c.post_id = p.id
            WHERE p.user_id = ? AND c.user_id != ?
            GROUP BY c.user_id
            UNION ALL
            SELECT r.user_id, 0, COUNT(*)
            FROM reactions r JOIN posts p ON r.post_id = p.id
            WHERE p.user_id = ? AND r.user_id != ?
            GROUP BY r.user_id
        ) GROUP BY actor_id
    ''', (user_id, user_id, user_id, user_id))

    favors = []
    for row in received:
        actor_id = row['actor_id']
        got = row['n_comments'] + row['n_reactions']

        # How many times I engaged with this person's posts.
        given = query_db('''
            SELECT (SELECT COUNT(*) FROM comments c JOIN posts p ON c.post_id = p.id
                    WHERE p.user_id = ? AND c.user_id = ?)
                 + (SELECT COUNT(*) FROM reactions r JOIN posts p ON r.post_id = p.id
                    WHERE p.user_id = ? AND r.user_id = ?) AS given
        ''', (actor_id, user_id, actor_id, user_id), one=True)['given']

        if given >= got:
            continue  # Already even (or I gave more): nothing to return.

        # Their newest post that I have not commented on or reacted to.
        post = query_db('''
            SELECT p.id, p.content, p.created_at, u.username, u.id AS user_id
            FROM posts p JOIN users u ON p.user_id = u.id
            WHERE p.user_id = ?
              AND NOT EXISTS (SELECT 1 FROM comments  c WHERE c.post_id = p.id AND c.user_id = ?)
              AND NOT EXISTS (SELECT 1 FROM reactions r WHERE r.post_id = p.id AND r.user_id = ?)
            ORDER BY p.created_at DESC LIMIT 1
        ''', (actor_id, user_id, user_id), one=True)
        if not post:
            continue  # They have no post left that I could engage with.

        post = dict(post)
        post['content'], _ = moderate_content(post['content'])
        favors.append({'post': post, 'n_comments': row['n_comments'],
                       'n_reactions': row['n_reactions'], 'got': got, 'given': given,
                       'owed': got - given})

    # Biggest "debts" first, so the most generous members are shown first.
    favors.sort(key=lambda f: f['owed'], reverse=True)
    return favors[:limit]


def favor_flash_if_returned(user_id, post_id):
    """
    Called after a comment/reaction. If the post's author had engaged with the
    current user before, a flash message confirms the favor was returned,
    reinforcing the reciprocity norm (Claim 31).
    """
    post = query_db('SELECT p.user_id, u.username FROM posts p JOIN users u ON p.user_id = u.id WHERE p.id = ?',
                    (post_id,), one=True)
    if not post or post['user_id'] == user_id:
        return
    helped_me = query_db('''
        SELECT 1 FROM comments c JOIN posts p ON c.post_id = p.id WHERE p.user_id = ? AND c.user_id = ?
        UNION
        SELECT 1 FROM reactions r JOIN posts p ON r.post_id = p.id WHERE p.user_id = ? AND r.user_id = ?
    ''', (user_id, post['user_id'], user_id, post['user_id']), one=True)
    if helped_me:
        flash(f"🤝 You returned the favor to @{post['username']}!", 'info')


@app.route('/')
def feed():
    #  1. Get Pagination and Filter Parameters 
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1
    sort = request.args.get('sort', 'new').lower()
    show = request.args.get('show', 'all').lower()
    
    # Define how many posts to show per page
    POSTS_PER_PAGE = 10
    offset = (page - 1) * POSTS_PER_PAGE

    current_user_id = session.get('user_id')
    params = []

    #  2. Build the Query
    # Conditions are collected in a list so filters can be combined.
    conditions = []
    if show == 'following' and current_user_id:
        conditions.append("p.user_id IN (SELECT followed_id FROM follows WHERE follower_id = ?)")
        params.append(current_user_id)
    elif show == 'unanswered':
        # Assignment 1, Feature 1: only posts nobody has replied to yet.
        conditions.append(UNANSWERED_CONDITION)
        if current_user_id:
            # Hide the user's own posts: they can't be the one to answer them.
            conditions.append("p.user_id != ?")
            params.append(current_user_id)
    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    # Add the pagination parameters to the query arguments
    pagination_params = (POSTS_PER_PAGE, offset)

    if sort == 'popular':
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
                   IFNULL(r.total_reactions, 0) as total_reactions
            FROM posts p
            JOIN users u ON p.user_id = u.id
            LEFT JOIN (
                SELECT post_id, COUNT(*) as total_reactions FROM reactions GROUP BY post_id
            ) r ON p.id = r.post_id
            {where_clause}
            ORDER BY total_reactions DESC, p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)
    elif sort == 'recommended':
        posts = recommend(current_user_id, show == 'following' and current_user_id)
    else:  # Default sort is 'new'
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
            FROM posts p
            JOIN users u ON p.user_id = u.id
            {where_clause}
            ORDER BY p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)

    posts_data = []
    for post in posts:
        # Determine if the current user follows the poster
        followed_poster = False
        if current_user_id and post['user_id'] != current_user_id:
            follow_check = query_db(
                'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
                (current_user_id, post['user_id']),
                one=True
            )
            if follow_check:
                followed_poster = True

        # Determine if the current user reacted to this post and with what reaction
        user_reaction = None
        if current_user_id:
            reaction_check = query_db(
                'SELECT reaction_type FROM reactions WHERE user_id = ? AND post_id = ?',
                (current_user_id, post['id']),
                one=True
            )
            if reaction_check:
                user_reaction = reaction_check['reaction_type']

        reactions = query_db('SELECT reaction_type, COUNT(*) as count FROM reactions WHERE post_id = ? GROUP BY reaction_type', (post['id'],))
        comments_raw = query_db('SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC', (post['id'],))
        post_dict = dict(post)
        post_dict['content'], _ = moderate_content(post_dict['content'])
        comments_moderated = []
        for comment in comments_raw:
            comment_dict = dict(comment)
            comment_dict['content'], _ = moderate_content(comment_dict['content'])
            comments_moderated.append(comment_dict)
        posts_data.append({
            'post': post_dict,
            'reactions': reactions,
            'user_reaction': user_reaction,
            'followed_poster': followed_poster,
            'comments': comments_moderated,
            # Assignment 1, Feature 1: data for the "No replies yet" badge.
            'days_waiting': days_waiting(post_dict['created_at'])
        })

    #  3b. Assignment 1 widgets (only for logged-in users)
    goal_progress = None
    favors = []
    if current_user_id:
        goal_progress = get_goal_progress(current_user_id)   # Feature 2
        if page == 1:
            favors = get_favors_to_return(current_user_id)    # Feature 3

    #  4. Render Template with Pagination Info
    return render_template('feed.html.j2',
                           posts=posts_data,
                           current_sort=sort,
                           current_show=show,
                           page=page, # Pass current page number
                           per_page=POSTS_PER_PAGE, # Pass items per page
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES,
                           goal_progress=goal_progress,
                           favors=favors)

@app.route('/posts/new', methods=['POST'])
def add_post():
    """Handles creating a new post from the feed."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to create a post.', 'danger')
        return redirect(url_for('login'))

    # Get content from the submitted form
    content = request.form.get('content')

    # Assignment 2, Design Claim 20: before publishing, check the draft with
    # moderate_content(). If it breaks a rule, stop and show a reminder page
    # instead of posting (unless the user already confirmed on that page).
    if needs_reminder(content):
        return send_to_reminder('post', content)

    # The original text is stored; moderate_content() is applied when it is
    # displayed, so moderators in /admin can still see what was written.
    moderated_content = content

    # Basic validation to ensure post is not empty
    if moderated_content and moderated_content.strip():
        db = get_db()
        db.execute('INSERT INTO posts (user_id, content) VALUES (?, ?)',
                   (user_id, moderated_content))
        db.commit()
        flash('Your post was successfully created!', 'success')
        # Assignment 2, DC20: if this came from the review page, finish there.
        draft_done = finish_reminder(content)
        if draft_done:
            return redirect(draft_done)
    else:
        # This will catch empty posts or posts that were fully censored
        flash('Post cannot be empty or was fully censored.', 'warning')

    # Redirect back to the main feed to see the new post
    return redirect(url_for('feed'))
    
    
@app.route('/posts/<int:post_id>/delete', methods=['POST'])
def delete_post(post_id):
    """Handles deleting a post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a post.', 'danger')
        return redirect(url_for('login'))

    # Find the post in the database
    post = query_db('SELECT id, user_id FROM posts WHERE id = ?', (post_id,), one=True)

    # Check if the post exists and if the current user is the owner
    if not post:
        flash('Post not found.', 'danger')
        return redirect(url_for('feed'))

    if post['user_id'] != user_id:
        # Security check: prevent users from deleting others' posts
        flash('You do not have permission to delete this post.', 'danger')
        return redirect(url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    # To maintain database integrity, delete associated records first
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    # Finally, delete the post itself
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()

    flash('Your post was successfully deleted.', 'success')
    # Redirect back to the page the user came from, or the feed as a fallback
    return redirect(request.referrer or url_for('feed'))

@app.route('/u/<username>')
def user_profile(username):
    """Displays a user's profile page with moderated bio, posts, and latest comments."""
    
    user_raw = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user_raw:
        abort(404)

    user = dict(user_raw)
    moderated_bio, _ = moderate_content(user.get('profile', ''))
    user['profile'] = moderated_bio

    posts_raw = query_db('SELECT id, content, user_id, created_at FROM posts WHERE user_id = ? ORDER BY created_at DESC', (user['id'],))
    posts = []
    for post_raw in posts_raw:
        post = dict(post_raw)
        moderated_post_content, _ = moderate_content(post['content'])
        post['content'] = moderated_post_content
        posts.append(post)

    comments_raw = query_db('SELECT id, content, user_id, post_id, created_at FROM comments WHERE user_id = ? ORDER BY created_at DESC LIMIT 100', (user['id'],))
    comments = []
    for comment_raw in comments_raw:
        comment = dict(comment_raw)
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    followers_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE followed_id = ?', (user['id'],), one=True)['cnt']
    following_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE follower_id = ?', (user['id'],), one=True)['cnt']

    #  NEW: CHECK FOLLOW STATUS 
    is_currently_following = False # Default to False
    current_user_id = session.get('user_id')
    
    # We only need to check if a user is logged in
    if current_user_id:
        follow_relation = query_db(
            'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
            (current_user_id, user['id']),
            one=True
        )
        if follow_relation:
            is_currently_following = True
    # --

    return render_template('user_profile.html.j2', 
                           user=user, 
                           posts=posts, 
                           comments=comments,
                           followers_count=followers_count, 
                           following_count=following_count,
                           is_following=is_currently_following)
    

@app.route('/u/<username>/followers')
def user_followers(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    followers = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.follower_id = u.id
        WHERE f.followed_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=followers, title="Followers of")

@app.route('/u/<username>/following')
def user_following(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    following = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.followed_id = u.id
        WHERE f.follower_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=following, title="Users followed by")

@app.route('/posts/<int:post_id>')
def post_detail(post_id):
    """Displays a single post and its comments, with content moderation applied."""
    
    post_raw = query_db('''
        SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
        FROM posts p
        JOIN users u ON p.user_id = u.id
        WHERE p.id = ?
    ''', (post_id,), one=True)

    if not post_raw:
        # The abort function will stop the request and show a 404 Not Found page.
        abort(404)

    #  Moderation for the Main Post 
    # Convert the raw database row to a mutable dictionary
    post = dict(post_raw)
    # Unpack the tuple from moderate_content, we only need the moderated content string here
    moderated_post_content, _ = moderate_content(post['content'])
    post['content'] = moderated_post_content

    #  Fetch Reactions (No moderation needed) 
    reactions = query_db('''
        SELECT reaction_type, COUNT(*) as count
        FROM reactions
        WHERE post_id = ?
        GROUP BY reaction_type
    ''', (post_id,))

    #  Fetch and Moderate Comments 
    comments_raw = query_db('SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC', (post_id,))
    
    comments = [] # Create a new list for the moderated comments
    for comment_raw in comments_raw:
        comment = dict(comment_raw) # Convert to a dictionary
        # Moderate the content of each comment
        print(comment['content'])
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    # Pass the moderated data to the template
    return render_template('post_detail.html.j2',
                           post=post,
                           reactions=reactions,
                           comments=comments,
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES)

@app.route('/about')
def about():
    return render_template('about.html.j2')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html.j2')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        location = request.form.get('location', '')
        birthdate = request.form.get('birthdate', '')
        profile = request.form.get('profile', '')

        hashed_password = generate_password_hash(password)

        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                'INSERT INTO users (username, password, location, birthdate, profile) VALUES (?, ?, ?, ?, ?)',
                (username, hashed_password, location, birthdate, profile)
            )
            db.commit()

            # 1. Get the ID of the user we just created.
            new_user_id = cur.lastrowid

            # 2. Add user info to the session cookie.
            session.clear() # Clear any old session data
            session['user_id'] = new_user_id
            session['username'] = username

            # 3. Flash a welcome message and redirect to the feed.
            flash(f'Welcome, {username}! Your account has been created.', 'success')
            return redirect(url_for('feed')) # Redirect to the main feed/dashboard

        except sqlite3.IntegrityError:
            flash('Username already taken. Please choose another one.', 'danger')
        finally:
            cur.close()
            db.close()
            
    return render_template('signup.html.j2')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        db.close()

        # 1. Check if the user exists.
        # 2. If user exists, use check_password_hash to securely compare the password.
        #    This function handles the salt and prevents timing attacks.
        if user and check_password_hash(user['password'], password):
            # Password is correct!
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash('Logged in successfully.', 'success')
            return redirect(url_for('feed'))
        else:
            # User does not exist or password was incorrect.
            flash('Invalid username or password.', 'danger')
            
    return render_template('login.html.j2')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/posts/<int:post_id>/comment', methods=['POST'])
def add_comment(post_id):
    """Handles adding a new comment to a specific post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to comment.', 'danger')
        return redirect(url_for('login'))

    # Get content from the submitted form
    content = request.form.get('content')

    # Assignment 2, Design Claim 20: same point-of-action reminder as for posts.
    if needs_reminder(content):
        return send_to_reminder('comment', content, post_id=post_id)

    # Basic validation to ensure comment is not empty
    if content and content.strip():
        db = get_db()
        db.execute('INSERT INTO comments (post_id, user_id, content) VALUES (?, ?, ?)',
                   (post_id, user_id, content))
        db.commit()
        flash('Your comment was added.', 'success')
        # Assignment 1, Feature 3: confirm when this comment returned a favor.
        favor_flash_if_returned(user_id, post_id)
        # Assignment 2, DC20: if this came from the review page, finish there.
        draft_done = finish_reminder(content)
        if draft_done:
            return redirect(draft_done)
    else:
        flash('Comment cannot be empty.', 'warning')

    # Redirect back to the page the user came from (likely the post detail page)
    return redirect(request.referrer or url_for('post_detail', post_id=post_id))

@app.route('/comments/<int:comment_id>/delete', methods=['POST'])
def delete_comment(comment_id):
    """Handles deleting a comment."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a comment.', 'danger')
        return redirect(url_for('login'))

    # Find the comment and the original post's author ID
    comment = query_db('''
        SELECT c.id, c.user_id, p.user_id as post_author_id
        FROM comments c
        JOIN posts p ON c.post_id = p.id
        WHERE c.id = ?
    ''', (comment_id,), one=True)

    # Check if the comment exists
    if not comment:
        flash('Comment not found.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # Security Check: Allow deletion if the user is the comment's author OR the post's author
    if user_id != comment['user_id'] and user_id != comment['post_author_id']:
        flash('You do not have permission to delete this comment.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()

    flash('Comment successfully deleted.', 'success')
    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/react', methods=['POST'])
def add_reaction():
    """Handles adding a new reaction or updating an existing one."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to react.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')
    new_reaction_type = request.form.get('reaction')

    if not post_id or not new_reaction_type:
        flash("Invalid reaction request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Step 1: Check if a reaction from this user already exists on this post.
    existing_reaction = query_db('SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
                                 (post_id, user_id), one=True)

    if existing_reaction:
        # Step 2: If it exists, UPDATE the reaction_type.
        db.execute('UPDATE reactions SET reaction_type = ? WHERE id = ?',
                   (new_reaction_type, existing_reaction['id']))
    else:
        # Step 3: If it does not exist, INSERT a new reaction.
        db.execute('INSERT INTO reactions (post_id, user_id, reaction_type) VALUES (?, ?, ?)',
                   (post_id, user_id, new_reaction_type))

    db.commit()

    # Assignment 1, Feature 3: confirm when a NEW reaction returned a favor.
    if not existing_reaction:
        favor_flash_if_returned(user_id, post_id)

    return redirect(request.referrer or url_for('feed'))

@app.route('/unreact', methods=['POST'])
def unreact():
    """Handles removing a user's reaction from a post."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to unreact.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')

    if not post_id:
        flash("Invalid unreact request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Remove the reaction if it exists
    existing_reaction = query_db(
        'SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
        (post_id, user_id),
        one=True
    )

    if existing_reaction:
        db.execute('DELETE FROM reactions WHERE id = ?', (existing_reaction['id'],))
        db.commit()
        flash("Reaction removed.", "success")
    else:
        flash("No reaction to remove.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/follow', methods=['POST'])
def follow_user(user_id):
    """Handles the logic for the current user to follow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to follow users.", "danger")
        return redirect(url_for('login'))

    # Security: Prevent users from following themselves
    if follower_id == user_id:
        flash("You cannot follow yourself.", "warning")
        return redirect(request.referrer or url_for('feed'))

    # Check if the user to be followed actually exists
    user_to_follow = query_db('SELECT id FROM users WHERE id = ?', (user_id,), one=True)
    if not user_to_follow:
        flash("The user you are trying to follow does not exist.", "danger")
        return redirect(request.referrer or url_for('feed'))
        
    db = get_db()
    try:
        # Insert the follow relationship. The PRIMARY KEY constraint will prevent duplicates if you've set one.
        db.execute('INSERT INTO follows (follower_id, followed_id) VALUES (?, ?)',
                   (follower_id, user_id))
        db.commit()
        username_to_follow = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You are now following {username_to_follow}.", "success")
    except sqlite3.IntegrityError:
        flash("You are already following this user.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/unfollow', methods=['POST'])
def unfollow_user(user_id):
    """Handles the logic for the current user to unfollow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to unfollow users.", "danger")
        return redirect(url_for('login'))

    db = get_db()
    cur = db.execute('DELETE FROM follows WHERE follower_id = ? AND followed_id = ?',
               (follower_id, user_id))
    db.commit()

    if cur.rowcount > 0:
        # cur.rowcount tells us if a row was actually deleted
        username_unfollowed = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You have unfollowed {username_unfollowed}.", "success")
    else:
        # This case handles if someone tries to unfollow a user they weren't following
        flash("You were not following this user.", "info")

    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/admin')
def admin_dashboard():
    """Displays the admin dashboard with users, posts, and comments, sorted by risk."""

    if session.get('username') != 'admin':
        flash("You do not have permission to access this page.", "danger")
        return redirect(url_for('feed'))

    RISK_LEVELS = { "HIGH": 5, "MEDIUM": 3, "LOW": 1 }
    PAGE_SIZE = 50

    def get_risk_profile(score):
        if score >= RISK_LEVELS["HIGH"]:
            return "HIGH", 3
        elif score >= RISK_LEVELS["MEDIUM"]:
            return "MEDIUM", 2
        elif score >= RISK_LEVELS["LOW"]:
            return "LOW", 1
        return "NONE", 0

    # Get pagination and current tab parameters
    try:
        users_page = int(request.args.get('users_page', 1))
        posts_page = int(request.args.get('posts_page', 1))
        comments_page = int(request.args.get('comments_page', 1))
    except ValueError:
        users_page = 1
        posts_page = 1
        comments_page = 1
    
    current_tab = request.args.get('tab', 'users') # Default to 'users' tab

    users_offset = (users_page - 1) * PAGE_SIZE
    
    # First, get all users to calculate risk, then apply pagination in Python
    # It's more complex to do this efficiently in SQL if risk calc is Python-side
    all_users_raw = query_db('SELECT id, username, profile, created_at FROM users')
    all_users = []
    for user in all_users_raw:
        user_dict = dict(user)
        user_risk_score = user_risk_analysis(user_dict['id'])
        risk_label, risk_sort_key = get_risk_profile(user_risk_score)
        user_dict['risk_label'] = risk_label
        user_dict['risk_sort_key'] = risk_sort_key
        user_dict['risk_score'] = min(5.0, round(user_risk_score, 2))
        all_users.append(user_dict)

    all_users.sort(key=lambda x: x['risk_score'], reverse=True)
    total_users = len(all_users)
    users = all_users[users_offset : users_offset + PAGE_SIZE]
    total_users_pages = (total_users + PAGE_SIZE - 1) // PAGE_SIZE

    # --- Posts Tab Data ---
    posts_offset = (posts_page - 1) * PAGE_SIZE
    total_posts_count = query_db('SELECT COUNT(*) as count FROM posts', one=True)['count']
    total_posts_pages = (total_posts_count + PAGE_SIZE - 1) // PAGE_SIZE

    posts_raw = query_db(f'''
        SELECT p.id, p.content, p.created_at, u.username, u.created_at as user_created_at
        FROM posts p JOIN users u ON p.user_id = u.id
        ORDER BY p.id DESC -- Order by ID for consistent pagination before risk sort
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, posts_offset))
    posts = []
    for post in posts_raw:
        post_dict = dict(post)
        _, base_score = moderate_content(post_dict['content'])
        final_score = base_score 
        author_created_dt = post_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            final_score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(final_score)
        post_dict['risk_label'] = risk_label
        post_dict['risk_sort_key'] = risk_sort_key
        post_dict['risk_score'] = round(final_score, 2)
        posts.append(post_dict)

    posts.sort(key=lambda x: x['risk_score'], reverse=True) # Sort after fetching and scoring

    # --- Comments Tab Data ---
    comments_offset = (comments_page - 1) * PAGE_SIZE
    total_comments_count = query_db('SELECT COUNT(*) as count FROM comments', one=True)['count']
    total_comments_pages = (total_comments_count + PAGE_SIZE - 1) // PAGE_SIZE

    comments_raw = query_db(f'''
        SELECT c.id, c.content, c.created_at, u.username, u.created_at as user_created_at
        FROM comments c JOIN users u ON c.user_id = u.id
        ORDER BY c.id DESC -- Order by ID for consistent pagination before risk sort
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, comments_offset))
    comments = []
    for comment in comments_raw:
        comment_dict = dict(comment)
        _, score = moderate_content(comment_dict['content'])
        author_created_dt = comment_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(score)
        comment_dict['risk_label'] = risk_label
        comment_dict['risk_sort_key'] = risk_sort_key
        comment_dict['risk_score'] = round(score, 2)
        comments.append(comment_dict)

    comments.sort(key=lambda x: x['risk_score'], reverse=True) # Sort after fetching and scoring


    return render_template('admin.html.j2', 
                           users=users, 
                           posts=posts, 
                           comments=comments,
                           
                           # Pagination for Users
                           users_page=users_page,
                           total_users_pages=total_users_pages,
                           users_has_next=(users_page < total_users_pages),
                           users_has_prev=(users_page > 1),

                           # Pagination for Posts
                           posts_page=posts_page,
                           total_posts_pages=total_posts_pages,
                           posts_has_next=(posts_page < total_posts_pages),
                           posts_has_prev=(posts_page > 1),

                           # Pagination for Comments
                           comments_page=comments_page,
                           total_comments_pages=total_comments_pages,
                           comments_has_next=(comments_page < total_comments_pages),
                           comments_has_prev=(comments_page > 1),

                           current_tab=current_tab,
                           PAGE_SIZE=PAGE_SIZE)



@app.route('/admin/delete/user/<int:user_id>', methods=['POST'])
def admin_delete_user(user_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))
        
    if user_id == session.get('user_id'):
        flash('You cannot delete your own account from the admin panel.', 'danger')
        return redirect(url_for('admin_dashboard'))
    
    db = get_db()
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    db.commit()
    flash(f'User {user_id} and all their content has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/post/<int:post_id>', methods=['POST'])
def admin_delete_post(post_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()
    flash(f'Post {post_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/comment/<int:comment_id>', methods=['POST'])
def admin_delete_comment(comment_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()
    flash(f'Comment {comment_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/rules')
def rules():
    return render_template('rules.html.j2')


# =====================================================================
# CODING ASSIGNMENT 2 - Design Claim 20 (Chapter 4):
# "Offering people reminders at the point of an action that may violate
#  norms reduces the number of offenses."
#
# When a user submits a post or comment that breaks a rule, it is NOT
# published straight away. The user is sent to a "review" page that shows
# which rules the draft breaks and how it would look after moderation, and
# can then edit it, cancel, or (for mild issues only) post it anyway.
# =====================================================================

def explain_violations(content):
    """
    Lists, in plain language, which moderation rules a draft breaks.
    Uses the same compiled regexes as moderate_content(), so the reminder
    always matches what the moderation will actually do.
    Returns (list_of_messages, is_severe). Severe drafts cannot be posted.
    """
    if not content:
        return [], False
    if TIER1_REGEX and TIER1_REGEX.search(content):
        return ['It contains language that is not allowed on MiniSocial at all. '
                'The whole text would be removed.'], True
    if TIER2_REGEX and TIER2_REGEX.search(content):
        return ['It looks like spam or a scam. The whole text would be removed.'], True

    issues = []
    links = len(URL_REGEX.findall(content))
    if links:
        issues.append(f'It contains {links} external link{"s" if links > 1 else ""}, '
                      f'which will be replaced with "{LINK_REPLACEMENT}".')
    profanity = len(TIER3_REGEX.findall(content)) if TIER3_REGEX else 0
    if profanity:
        issues.append(f'It contains {profanity} word{"s" if profanity > 1 else ""} from the '
                      f'mild-profanity list, which will be shown as ****.')
    letters = [ch for ch in content if ch.isalpha()]
    if len(letters) > CAPS_MIN_LETTERS and sum(ch.isupper() for ch in letters) / len(letters) > CAPS_RATIO:
        issues.append('It is written mostly in CAPITAL LETTERS, which reads as shouting.')
    return issues, False


def needs_reminder(content):
    """
    True if the draft should be stopped and reviewed before publishing.
    - Severe drafts (Tier 1/2) are always stopped: they would be removed anyway.
    - Mild drafts are stopped once; if the user presses "Post anyway" on the
      review page, the form carries confirmed=1 and the draft goes through.
    """
    if not content or not content.strip():
        return False
    _, score = moderate_content(content)
    if score == 0:
        return False
    _, severe = explain_violations(content)
    return severe or request.form.get('confirmed') != '1'


def send_to_reminder(kind, content, post_id=None):
    """Keeps the draft in the session and redirects to the review page."""
    return_to = request.referrer or url_for('feed')
    previous = session.get('pending_draft')
    if request.form.get('from_review') == '1' and previous:
        # Re-checked from the review page: keep the ORIGINAL page to return to.
        return_to = previous.get('return_to', return_to)
    session['pending_draft'] = {
        'kind': kind,                      # 'post' or 'comment'
        'content': content,
        'post_id': post_id,                # only for comments
        'return_to': return_to,
    }
    return redirect(url_for('review_draft'))


def finish_reminder(content):
    """
    Called after a post/comment is saved. If it was a draft from the review
    page, clears it, thanks the user if they fixed it (face-saving, positive
    feedback), and returns the page they originally came from. Otherwise None.
    """
    draft = session.pop('pending_draft', None)
    # Only drafts submitted FROM the review page count (from_review=1);
    # an old draft left in the session must not affect a normal new post.
    if not draft or request.form.get('from_review') != '1':
        return None
    if moderate_content(content)[1] == 0:
        flash('Thanks for editing! Your text now follows the community rules.', 'success')
    return draft.get('return_to') or url_for('feed')


@app.route('/review', methods=['GET'])
def review_draft():
    """
    The point-of-action reminder page. Shows the user's draft, the rules it
    breaks, and a preview of how it would appear after moderation.
    The edit form posts back to the normal add_post/add_comment route, so an
    edited draft is checked again automatically.
    """
    if not session.get('user_id'):
        return redirect(url_for('login'))
    draft = session.get('pending_draft')
    if not draft:
        return redirect(url_for('feed'))

    issues, severe = explain_violations(draft['content'])
    preview, score = moderate_content(draft['content'])
    if draft['kind'] == 'post':
        action_url = url_for('add_post')
    else:
        action_url = url_for('add_comment', post_id=draft['post_id'])

    return render_template('review_draft.html.j2',
                           draft=draft, issues=issues, severe=severe,
                           preview=preview, score=score, action_url=action_url)


@app.route('/review/cancel', methods=['POST'])
def cancel_draft():
    """Discards the pending draft and returns to where the user came from."""
    draft = session.pop('pending_draft', None)
    flash('Your draft was discarded.', 'info')
    return redirect((draft or {}).get('return_to') or url_for('feed'))


# ---------- Assignment 1, Feature 2: the "My Goals" page ----------
@app.route('/goals', methods=['GET', 'POST'])
def goals():
    """
    GET  shows the user's weekly goal, progress, deadline, the system's
         suggested goal and a comparison with the rest of the community.
    POST saves a new self-set goal, or the suggested one.
    """
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to set goals.', 'danger')
        return redirect(url_for('login'))

    suggestion = suggest_goal(user_id)

    if request.method == 'POST':
        if request.form.get('use_suggested'):
            posts_goal, comments_goal = suggestion['posts'], suggestion['comments']
        else:
            try:
                posts_goal = int(request.form.get('posts_goal', 0))
                comments_goal = int(request.form.get('comments_goal', 0))
            except ValueError:
                flash('Goals must be whole numbers.', 'warning')
                return redirect(url_for('goals'))
            # A goal of 0 is not a goal, and a huge number is not realistic.
            if not (1 <= posts_goal <= 100 and 1 <= comments_goal <= 200):
                flash('Choose between 1-100 posts and 1-200 comments per week.', 'warning')
                return redirect(url_for('goals'))

        # Insert or replace the user's single goal row (SQLite "upsert").
        db = get_db()
        db.execute('''
            INSERT INTO user_goals (user_id, posts_goal, comments_goal, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                posts_goal = excluded.posts_goal,
                comments_goal = excluded.comments_goal,
                updated_at = CURRENT_TIMESTAMP
        ''', (user_id, posts_goal, comments_goal))
        db.commit()
        flash(f'Goal saved: {posts_goal} posts and {comments_goal} comments this week. Go for it!', 'success')
        return redirect(url_for('goals'))

    return render_template('goals.html.j2',
                           progress=get_goal_progress(user_id),
                           suggestion=suggestion,
                           comparison=community_comparison(user_id))

@app.template_global()
def loop_color(user_id):
    # Generate a pastel color based on user_id hash
    h = hashlib.md5(str(user_id).encode()).hexdigest()
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return f'rgb({r % 128 + 80}, {g % 128 + 80}, {b % 128 + 80})'


# ----- Functions to be implemented are below
# Coding Assignment #2

# --- Moderation rule constants (values from the /rules page) ---
SEVERE_SCORE = 5.0              # Rules 1.1.1 / 1.1.2: fixed score for removed content
TIER3_PENALTY = 2.0             # Rule 1.2.1: per mild-profanity match
LINK_PENALTY = 2.0              # Rule 1.2.2: per external link
CAPS_PENALTY = 0.5              # Rule 1.2.3: excessive capitalization
CAPS_MIN_LETTERS = 15           # Rule 1.2.3: applies only if MORE than 15 letters...
CAPS_RATIO = 0.70               # ...and MORE than 70% of them are uppercase
TIER1_REMOVED_TEXT = '[content removed due to severe violation]'
TIER2_REMOVED_TEXT = '[content removed due to spam/scam policy]'
LINK_REPLACEMENT = '[link removed]'


def build_whole_word_regex(terms):
    """
    Builds ONE case-insensitive regex that matches any of the given words or
    phrases as whole words/phrases.

    - (?<!\\w) and (?!\\w) are used instead of \\b because some phrases start or
      end with non-letters (e.g. "100% free"), where \\b would fail.
    - Longest terms come first, so a multi-word phrase wins over a single word
      it contains and the same text is never counted twice.
    - Spaces inside phrases match any whitespace ("risk  free" with 2 spaces).
    """
    if not terms:
        return None
    escaped = [re.escape(t.strip()).replace(r'\ ', r'\s+')
               for t in sorted(terms, key=len, reverse=True) if t.strip()]
    return re.compile(r'(?<!\w)(?:' + '|'.join(escaped) + r')(?!\w)', re.IGNORECASE)


# Compiled once at start-up: the admin page moderates thousands of texts per load.
TIER1_REGEX = build_whole_word_regex(TIER1_WORDS)
TIER2_REGEX = build_whole_word_regex(TIER2_PHRASES)
TIER3_REGEX = build_whole_word_regex(TIER3_WORDS)

# Rule 1.2.2: what counts as a URL. Matches
#   - anything starting with http:// or https://
#   - anything starting with www.
#   - bare domains with a common top-level domain, e.g. "cheap-pills.biz/offer"
# Trailing punctuation (".", ",", ")", "!" ...) is left outside the match, so
# "see example.com." keeps its final full stop.
URL_REGEX = re.compile(
    r'(?:https?://|www\.)[^\s<>"\']+?(?=[.,!?;:)\]]*(?:\s|$))'
    r'|(?<![\w@.-])[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|info|biz|io|co|xyz|ru|fi|gr|eu|uk|de|ly|me|tk|top|online|site|shop)'
    r'(?:/[^\s<>"\']*?)?(?=[.,!?;:)\]]*(?:\s|$))',
    re.IGNORECASE)


# Assignment 2.2
def user_risk_analysis(user_id):
    """
    Args:
        user_id: The ID of the user on which we perform risk analysis.

    Returns:
        A float number score showing the risk associated with this user. There are no strict rules or bounds to this score, other than that a score of less than 1.0 means no risk, 1.0 to 3.0 is low risk, 3.0 to 5.0 is medium risk and above 5.0 is high risk. (An upper bound of 5.0 is applied to this score elsewhere in the codebase)

        You will be able to check the scores by logging in with the administrator account:
            username: admin
            password: admin
        Then, navigate to the /admin endpoint. (http://localhost:8080/admin)

    Implementation follows Rule 2.2 on the /rules page, step by step.
    """
    user = query_db('SELECT profile, created_at FROM users WHERE id = ?', (user_id,), one=True)
    if not user:
        return 0.0

    # Step 1: Content Score of the profile text.
    _, profile_score = moderate_content(user['profile'])

    # Step 2: average Content Score of all the user's posts (0 if no posts).
    posts = query_db('SELECT content FROM posts WHERE user_id = ?', (user_id,))
    post_scores = [moderate_content(p['content'])[1] for p in posts]
    average_post_score = sum(post_scores) / len(post_scores) if post_scores else 0.0

    # Step 3: average Content Score of all the user's comments (0 if none).
    comments = query_db('SELECT content FROM comments WHERE user_id = ?', (user_id,))
    comment_scores = [moderate_content(c['content'])[1] for c in comments]
    average_comment_score = sum(comment_scores) / len(comment_scores) if comment_scores else 0.0

    # Step 4: weighted combination. Posts weigh 3x because they are the most
    # visible content on the platform.
    content_risk_score = (profile_score * 1) + (average_post_score * 3) + (average_comment_score * 1)

    # Step 5: new accounts are riskier (throwaway / spam accounts).
    age_days = account_age_days(user['created_at'])
    if age_days < 7:
        user_risk_score = content_risk_score * 1.5
    elif age_days < 30:
        user_risk_score = content_risk_score * 1.2
    else:
        user_risk_score = content_risk_score

    # Step 6: cap at 5.0.
    return min(user_risk_score, 5.0)


def account_age_days(created_at):
    """
    Returns the age of an account in whole days. created_at comes from SQLite
    either as a datetime (PARSE_DECLTYPES) or a string. Timestamps are stored
    in UTC, so utcnow() is used. A missing date counts as an old account.
    """
    if isinstance(created_at, str):
        try:
            created_at = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            return 9999
    if not isinstance(created_at, datetime):
        return 9999
    return (datetime.utcnow() - created_at).days

    
# Assignment 2.1
def moderate_content(content):
    """
    Args
        content: the text content of a post or comment to be moderated.
        
    Returns: 
        A tuple containing the moderated content (string) and a severity score (float). There are no strict rules or bounds to the severity score, other than that a score of less than 1.0 means no risk, 1.0 to 3.0 is low risk, 3.0 to 5.0 is medium risk and above 5.0 is high risk.
    
    This function moderates a string of content and calculates a severity score based on
    rules loaded from the 'censorship.dat' file. These are already loaded as TIER1_WORDS, TIER2_PHRASES and TIER3_WORDS. Tier 1 corresponds to strong profanity, Tier 2 to scam/spam phrases and Tier 3 to mild profanity.
    
    You will be able to check the scores by logging in with the administrator account:
            username: admin
            password: admin
    Then, navigate to the /admin endpoint. (http://localhost:8080/admin)

    Implementation follows Part 1 of the /rules page, in the order given there.
    """
    # Empty profiles/posts come from the database as None or "".
    if not content:
        return '', 0.0

    # ---- Stage 1.1: severe violations -> remove everything, fixed score ----
    # Rule 1.1.1: any Tier 1 word removes the whole content.
    if TIER1_REGEX and TIER1_REGEX.search(content):
        return TIER1_REMOVED_TEXT, SEVERE_SCORE
    # Rule 1.1.2: only checked when there was no Tier 1 match.
    if TIER2_REGEX and TIER2_REGEX.search(content):
        return TIER2_REMOVED_TEXT, SEVERE_SCORE

    # ---- Stage 1.2: scored violations, score starts at 0.0 ----
    score = 0.0
    moderated_content = content

    # Rule 1.2.2: external links. Done BEFORE Tier 3 so that a word hidden
    # inside a URL is not counted twice (once as a word, once as a link).
    moderated_content, link_count = URL_REGEX.subn(LINK_REPLACEMENT, moderated_content)
    score += link_count * LINK_PENALTY

    # Rule 1.2.1: Tier 3 words -> asterisks of the same length, +2.0 each.
    # subn() returns how many replacements were made = number of matches.
    moderated_content, tier3_count = TIER3_REGEX.subn(
        lambda m: '*' * len(m.group(0)), moderated_content) if TIER3_REGEX else (moderated_content, 0)
    score += tier3_count * TIER3_PENALTY

    # Rule 1.2.3: excessive capitalization. Measured on the ORIGINAL text
    # (asterisks would hide letters); the content itself is not modified.
    letters = [ch for ch in content if ch.isalpha()]
    if len(letters) > CAPS_MIN_LETTERS:
        uppercase_ratio = sum(ch.isupper() for ch in letters) / len(letters)
        if uppercase_ratio > CAPS_RATIO:
            score += CAPS_PENALTY

    return moderated_content, score

# Coding Assignment #3
# Assignment 3.1
def recommend(user_id, filter_following):
    """
    Args:
        user_id: The ID of the current user.
        filter_following: Boolean, True if we only want to see recommendations from followed users.

    Returns:
        A list of 5 recommended posts, in reverse-chronological order.

    To test whether your recommendation algorithm works, let's pretend we like the DIY topic. Here are some users that often post DIY comment and a few example posts. Make sure your account did not engage with anything else. You should test your algorithm with these and see if your recommendation algorithm picks up on your interest in DIY and starts showing related content.
    
    Users: @starboy99, @DancingDolphin, @blogger_bob
    Posts: 1810, 1875, 1880, 2113
    
    Materials: 
    - https://www.nvidia.com/en-us/glossary/recommendation-system/
    - http://www.configworks.com/mz/handout_recsys_sac2010.pdf
    - https://www.researchgate.net/publication/227268858_Recommender_Systems_Handbook
    """

    recommended_posts = {} 

    return recommended_posts;

if __name__ == '__main__':
    app.run(debug=True, port=8080)