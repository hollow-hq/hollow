CREATE TABLE IF NOT EXISTS guild_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER UNIQUE NOT NULL,
    prefix TEXT NOT NULL DEFAULT ','
);

CREATE TABLE IF NOT EXISTS user_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER UNIQUE NOT NULL,
    prefix TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    emoji_approve TEXT DEFAULT '<:approve:1547570974457733141>',
    emoji_deny TEXT DEFAULT '<:deny:1547570956191535144>',
    emoji_warn TEXT DEFAULT '<:warning:1547570970791772270>',
    emoji_cooldown TEXT DEFAULT '<:cooldown:1547572522004914218>',
    neutral_color INTEGER DEFAULT 0x2B2D31
);

INSERT OR IGNORE INTO bot_config (id, emoji_approve, emoji_deny, emoji_warn, emoji_cooldown, neutral_color)
VALUES (1, '<:approve:1547570974457733141>', '<:deny:1547570956191535144>', '<:warning:1547570970791772270>', '<:cooldown:1547572522004914218>', 0x2B2D31);

CREATE TABLE IF NOT EXISTS aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    command_name TEXT NOT NULL,
    shortcut TEXT NOT NULL,
    UNIQUE(guild_id, shortcut)
);
-- ── cogs/server ──

CREATE TABLE IF NOT EXISTS welcome_config (
    guild_id INTEGER PRIMARY KEY,
    channels TEXT NOT NULL DEFAULT '[]',
    message TEXT,
    dm_enabled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS leave_config (
    guild_id INTEGER PRIMARY KEY,
    channels TEXT NOT NULL DEFAULT '[]',
    message TEXT
);

CREATE TABLE IF NOT EXISTS log_config (
    guild_id INTEGER PRIMARY KEY,
    category_id INTEGER,
    channels TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS ignore_config (
    guild_id INTEGER PRIMARY KEY,
    users TEXT NOT NULL DEFAULT '[]',
    channels TEXT NOT NULL DEFAULT '[]',
    roles TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS fake_permission_config (
    guild_id INTEGER PRIMARY KEY,
    permissions TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS sticky_config (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER PRIMARY KEY,
    message TEXT NOT NULL,
    last_message_id INTEGER,
    created_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS mod_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    moderator_id INTEGER,
    reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS admin_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    note TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS webhook_store (
    identifier TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    webhook_id INTEGER NOT NULL,
    webhook_token TEXT NOT NULL,
    webhook_url TEXT NOT NULL,
    creator_id INTEGER NOT NULL,
    name TEXT,
    locked INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── cogs/moderation ──

CREATE TABLE IF NOT EXISTS warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER,
    reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reactmute (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS imagemute (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS forcenick (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    forced_nick TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS lock_snapshot (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    was_locked INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS ghostping_config (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    delay INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (guild_id, channel_id)
);

-- ── cogs/fun ──

CREATE TABLE IF NOT EXISTS juul_stats (
    guild_id TEXT PRIMARY KEY,
    data TEXT,
    enabled INTEGER,
    flavor TEXT,
    holder_id TEXT,
    hits INTEGER DEFAULT 0,
    passes INTEGER DEFAULT 0,
    steals INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS juul_users (
    guild_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    hits INTEGER DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

-- ── cogs/automation ──

CREATE TABLE IF NOT EXISTS autorole_config (
    guild_id INTEGER PRIMARY KEY,
    everyone TEXT NOT NULL DEFAULT '[]',
    humans TEXT NOT NULL DEFAULT '[]',
    bots TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS autoreact_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    trigger TEXT NOT NULL,
    emoji TEXT NOT NULL,
    exclusive_channels TEXT NOT NULL DEFAULT '[]',
    exclusive_roles TEXT NOT NULL DEFAULT '[]',
    UNIQUE(guild_id, trigger)
);

CREATE TABLE IF NOT EXISTS trigger_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    trigger TEXT NOT NULL,
    response TEXT NOT NULL,
    exclusive_channels TEXT NOT NULL DEFAULT '[]',
    exclusive_roles TEXT NOT NULL DEFAULT '[]',
    UNIQUE(guild_id, trigger)
);

CREATE TABLE IF NOT EXISTS image_search_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    query TEXT NOT NULL,
    image_data TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- -- cogs/server (giveaway) --

CREATE TABLE IF NOT EXISTS giveaways (
    message_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    host_id INTEGER NOT NULL,
    prize TEXT NOT NULL,
    winners INTEGER NOT NULL DEFAULT 1,
    duration_seconds INTEGER NOT NULL,
    minimum_age_seconds INTEGER NOT NULL DEFAULT 0,
    required_role_id INTEGER,
    entries TEXT NOT NULL DEFAULT '[]',
    ended INTEGER NOT NULL DEFAULT 0,
    winner_ids TEXT NOT NULL DEFAULT '[]',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ends_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS giveaway_config (
    guild_id INTEGER PRIMARY KEY,
    default_channel_id INTEGER
);

CREATE TABLE IF NOT EXISTS saved_embeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    script TEXT NOT NULL,
    UNIQUE(user_id, name)
);

CREATE TABLE IF NOT EXISTS buttonroles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    button_style TEXT NOT NULL DEFAULT 'primary',
    label TEXT NOT NULL,
    emoji TEXT,
    user_limit INTEGER,
    UNIQUE(guild_id, message_id, role_id)
);

-- ── cogs/engagement ──

-- Suggestions: per-guild configuration plus the active "panel" message that
-- users click to open the suggestion modal.
CREATE TABLE IF NOT EXISTS suggestions_config (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER,
    panel_message_id INTEGER,
    panel_template TEXT,
    embed_template TEXT
);

-- Individual suggestions posted to the suggestion channel.
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER,
    message_id INTEGER UNIQUE,
    author_id INTEGER NOT NULL,
    suggestion TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', -- pending | approved | denied | implemented | considered
    responder_id INTEGER,
    response TEXT,
    response_message_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Block-list of members that cannot create suggestions.
CREATE TABLE IF NOT EXISTS suggestions_blacklist (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER,
    reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, user_id)
);

-- Invite tracker: one row per unique invite code that we have seen in a
-- guild, recording who created it and how often it has been used.
CREATE TABLE IF NOT EXISTS invites (
    guild_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    inviter_id INTEGER,
    channel_id INTEGER,
    url TEXT,
    uses INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, code)
);

-- Tracks which invite code a member joined through so we can attribute
-- future invites back to the correct inviter.
CREATE TABLE IF NOT EXISTS invite_joins (
    guild_id INTEGER NOT NULL,
    user_id INTEGER PRIMARY KEY,
    invite_code TEXT,
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Per-(guild, user) XP for the leveling system.
CREATE TABLE IF NOT EXISTS levels (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    xp INTEGER NOT NULL DEFAULT 0,
    total_xp INTEGER NOT NULL DEFAULT 0,
    last_message_ts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

-- Per-guild leveling configuration.
CREATE TABLE IF NOT EXISTS level_config (
    guild_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    xp_min INTEGER NOT NULL DEFAULT 15,
    xp_max INTEGER NOT NULL DEFAULT 25,
    cooldown_seconds INTEGER NOT NULL DEFAULT 60,
    level_up_message TEXT,
    level_up_channel_id INTEGER,
    announce INTEGER NOT NULL DEFAULT 1
);

-- Channels and roles excluded from gaining XP.
CREATE TABLE IF NOT EXISTS level_excludes (
    guild_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                       -- 'channel' | 'role'
    target_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, kind, target_id)
);

-- Role rewards awarded when a user reaches a given level.
CREATE TABLE IF NOT EXISTS level_rewards (
    guild_id INTEGER NOT NULL,
    level INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, level)
);

-- Starboard configuration. Up to 3 rows per guild, identified by name.
CREATE TABLE IF NOT EXISTS starboards (
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    emoji TEXT NOT NULL,
    threshold INTEGER NOT NULL,
    locked INTEGER NOT NULL DEFAULT 0,
    self_react INTEGER NOT NULL DEFAULT 0,
    color INTEGER,
    PRIMARY KEY (guild_id, name)
);

-- Channels/roles/users ignored by a starboard.
CREATE TABLE IF NOT EXISTS starboard_ignores (
    guild_id INTEGER NOT NULL,
    starboard_name TEXT NOT NULL,
    kind TEXT NOT NULL,                       -- 'channel' | 'role' | 'user'
    target_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, starboard_name, kind, target_id)
);

-- Tracks which messages already have a starboard embed so we don't repost.
CREATE TABLE IF NOT EXISTS starboard_posts (
    guild_id INTEGER NOT NULL,
    starboard_name TEXT NOT NULL,
    source_message_id INTEGER NOT NULL,
    starboard_message_id INTEGER NOT NULL,
    stargazers TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (guild_id, starboard_name, source_message_id)
);

-- -- cogs/engagement (giveaways & afk extensions) --

-- Per-guild extra entry roles: hold an additional number of entries per role.
CREATE TABLE IF NOT EXISTS giveaway_extra_entries (
    guild_id INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    entries INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (guild_id, role_id)
);

-- Required roles (per-guild) — server-wide list of roles that *can* be required
-- for a giveaway (set with `,giveaways edit requiredroles`).
CREATE TABLE IF NOT EXISTS giveaway_required_roles (
    guild_id INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);

-- Members blacklisted from entering giveaways in a particular guild.
CREATE TABLE IF NOT EXISTS giveaway_blacklist (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER,
    reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, user_id)
);

-- Per-user AFK status (one row per user across all guilds).
CREATE TABLE IF NOT EXISTS afk_users (
    user_id INTEGER PRIMARY KEY,
    reason TEXT NOT NULL,
    since TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_autoclear TIMESTAMP
);

-- Channels ignored by the AFK system on a per-guild basis (e.g. bot commands
-- channels where the user is allowed to talk while AFK).
CREATE TABLE IF NOT EXISTS afk_ignore_channels (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);

-- Per-guild AFK configuration: autoclear timeout and log channel.
CREATE TABLE IF NOT EXISTS afk_config (
    guild_id INTEGER PRIMARY KEY,
    timeout_minutes INTEGER NOT NULL DEFAULT 0,
    log_channel_id INTEGER
);

-- Per-user AFK presets saved for quick re-use.
CREATE TABLE IF NOT EXISTS afk_presets (
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (user_id, name)
);

-- Tracks how many times each command has been used globally.
CREATE TABLE IF NOT EXISTS command_usage (
    command_name TEXT PRIMARY KEY,
    usage_count INTEGER NOT NULL DEFAULT 0
);

-- ── cogs/antinuke ──
-- NOTE: existing databases are migrated at runtime by the antinuke cog
-- (cog_load adds missing columns via ALTER TABLE), so this file is the
-- source of truth for fresh installs while old rows keep working.
CREATE TABLE IF NOT EXISTS antinuke_config (
    guild_id INTEGER PRIMARY KEY,
    webhook_enabled INTEGER DEFAULT 0,
    webhook_threshold INTEGER DEFAULT 3,
    webhook_punishment TEXT DEFAULT 'ban',
    webhook_command INTEGER DEFAULT 0,
    channel_enabled INTEGER DEFAULT 0,
    channel_threshold INTEGER DEFAULT 3,
    channel_punishment TEXT DEFAULT 'ban',
    vanity_enabled INTEGER DEFAULT 0,
    vanity_punishment TEXT DEFAULT 'ban',
    ban_enabled INTEGER DEFAULT 0,
    ban_threshold INTEGER DEFAULT 3,
    ban_punishment TEXT DEFAULT 'ban',
    ban_command INTEGER DEFAULT 1,
    botadd_enabled INTEGER DEFAULT 0,
    emoji_enabled INTEGER DEFAULT 0,
    emoji_threshold INTEGER DEFAULT 3,
    emoji_punishment TEXT DEFAULT 'ban',
    role_enabled INTEGER DEFAULT 0,
    role_threshold INTEGER DEFAULT 3,
    role_punishment TEXT DEFAULT 'ban',
    role_command INTEGER DEFAULT 0,
    kick_enabled INTEGER DEFAULT 0,
    kick_threshold INTEGER DEFAULT 3,
    kick_punishment TEXT DEFAULT 'ban',
    kick_command INTEGER DEFAULT 1,
    permissions_enabled INTEGER DEFAULT 0,
    permissions_grant INTEGER DEFAULT 0,
    permissions_remove INTEGER DEFAULT 0
);

-- Per-permission watch settings for the `permissions` module.
-- One row per (guild, permission); each direction (grant/remove) is
-- tracked independently with its own threshold/punishment/command flag.
-- Valid `permission` values (exact Bleed list, 12 total):
-- administrator, ban_members, mention_everyone, kick_members,
-- moderate_members, manage_guild, manage_channels, manage_roles,
-- view_audit_log, manage_webhooks, manage_expressions, manage_nicknames
CREATE TABLE IF NOT EXISTS antinuke_permissions (
    guild_id INTEGER NOT NULL,
    permission TEXT NOT NULL,
    watch_grant INTEGER NOT NULL DEFAULT 0,
    watch_remove INTEGER NOT NULL DEFAULT 0,
    threshold INTEGER NOT NULL DEFAULT 3,
    punishment TEXT NOT NULL DEFAULT 'ban',
    command_detect INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (guild_id, permission)
);

CREATE TABLE IF NOT EXISTS antinuke_whitelist (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS antinuke_admins (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- ── cogs/voicemaster ──

CREATE TABLE IF NOT EXISTS vm_guild (
    guild_id INTEGER PRIMARY KEY,
    default_bitrate INTEGER NOT NULL DEFAULT 64,
    default_region TEXT NOT NULL DEFAULT 'us-west',
    default_role_id INTEGER,
    default_interface INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS vm_hub (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL DEFAULT 'VoiceMaster',
    category_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    UNIQUE(guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS vm_temp (
    channel_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    hub_id INTEGER,
    owner_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    locked INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    temporary INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── cogs/server (boost notifications) ──

CREATE TABLE IF NOT EXISTS boost_config (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER,
    message TEXT
);

-- ── cogs/server (booster roles) ──

-- Per-guild booster role configuration.
CREATE TABLE IF NOT EXISTS booster_role_config (
    guild_id INTEGER PRIMARY KEY,
    base_role_id INTEGER,
    enabled INTEGER NOT NULL DEFAULT 0,
    hoist INTEGER NOT NULL DEFAULT 0,
    role_limit INTEGER NOT NULL DEFAULT 1,
    share_limit INTEGER NOT NULL DEFAULT 3,
    default_color INTEGER NOT NULL DEFAULT 0x99AAB5,
    filtered_words TEXT NOT NULL DEFAULT '[]'
);

-- Every role owned as a "booster role" (created or included), along with who
-- owns it. One member can own multiple roles up to `role_limit`.
CREATE TABLE IF NOT EXISTS booster_roles (
    role_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Booster role shares: owners can grant their booster role to other members.
CREATE TABLE IF NOT EXISTS booster_role_shares (
    guild_id INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    sender_id INTEGER NOT NULL,
    recipient_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, role_id, recipient_id)
);


-- ── cogs/confess ──

-- Per-guild confession setup: which channel posts go to, total numbered count,
-- and the upvote/downvote reaction emoji (NULL = disabled).
CREATE TABLE IF NOT EXISTS confess (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    confession INTEGER NOT NULL DEFAULT 0,
    upvote TEXT,
    downvote TEXT
);

-- Records the author of every numbered confession. `last_confession_ts` is
-- also used for the 60s anti-spam cooldown on ;/confess.
CREATE TABLE IF NOT EXISTS confess_members (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    confession INTEGER NOT NULL,
    last_confession_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, confession)
);
CREATE INDEX IF NOT EXISTS confess_members_user_idx
    ON confess_members (guild_id, user_id);

-- Members blocked from sending confessions in a specific guild.
CREATE TABLE IF NOT EXISTS confess_mute (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- Per-guild word blacklist applied against confession bodies before posting.
CREATE TABLE IF NOT EXISTS confess_blacklist (
    guild_id INTEGER NOT NULL,
    word TEXT NOT NULL,
    PRIMARY KEY (guild_id, word)
);

-- Tracks every anonymous reply sent in a confession thread so reports and
-- moderation lookups can resolve the real author.
CREATE TABLE IF NOT EXISTS confess_replies (
    message_id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    guild_id INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS confess_replies_user_idx
    ON confess_replies (guild_id, user_id);

-- ── cogs/security ──
-- Merged Anti-Nuke + Anti-Raid cog. SQLite has no native JSON type, so all
-- per-module configuration blobs (thresholds / punishments) and identifier
-- lists (whitelists / admins) are stored as TEXT and serialised through
-- json.dumps / json.loads in the cog.

-- Per-guild anti-raid configuration. Each optional column holds a JSON blob
-- like {"threshold": 5, "punishment": "ban"} or NULL when that module is
-- disabled. `locked` mirrors the reference cog's `incident-actions` hack: it
-- is a coarse in-DB lockdown flag, separate from Discord's incident actions.
CREATE TABLE IF NOT EXISTS antiraid_config (
    guild_id INTEGER PRIMARY KEY,
    joins TEXT,        -- json   {"threshold": int, "punishment": str} | NULL
    mentions TEXT,     -- json   {"threshold": int, "punishment": str} | NULL
    avatar TEXT,       -- json   {"punishment": str}                   | NULL
    browser TEXT,      -- json   {"punishment": str}                   | NULL
    locked INTEGER NOT NULL DEFAULT 0
);

-- Per-guild anti-nuke configuration. Bot-protection is stored as a single
-- INTEGER (on/off). Every other module is a JSON blob describing its
-- threshold + punishment, or NULL when disabled.
CREATE TABLE IF NOT EXISTS antinuke_settings (
    guild_id INTEGER PRIMARY KEY,
    bot_enabled INTEGER NOT NULL DEFAULT 0,
    ban TEXT,        -- json | NULL
    kick TEXT,       -- json | NULL
    role TEXT,       -- json | NULL
    channel TEXT,    -- json | NULL
    webhook TEXT,    -- json | NULL
    emoji TEXT,      -- json | NULL
    whitelist TEXT NOT NULL DEFAULT '[]',  -- json [int, ...]
    admins TEXT NOT NULL DEFAULT '[]'      -- json [int, ...]
);

-- Every antinuke action the bot takes is recorded here so that
-- moderators can audit what happened even after the in-memory
-- debounce window has passed and the audit log entry has rolled off.
CREATE TABLE IF NOT EXISTS antinuke_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    module TEXT NOT NULL,          -- 'ban' | 'kick' | 'role' | 'channel' | 'webhook' | 'emoji' | 'bot'
    executor_id INTEGER NOT NULL,
    target_id INTEGER,
    punishment TEXT NOT NULL,
    success INTEGER NOT NULL,
    detail TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS antinuke_logs_guild_idx
    ON antinuke_logs (guild_id, created_at);
CREATE INDEX IF NOT EXISTS antinuke_logs_executor_idx
    ON antinuke_logs (guild_id, executor_id, created_at);

-- ── cogs/tickets ──
-- Per-guild ticket configuration: where tickets live, who can staff them,
-- what the panel says and how transcripts are delivered.
CREATE TABLE IF NOT EXISTS tickets_config (
    guild_id INTEGER PRIMARY KEY,
    category_id INTEGER,
    log_channel_id INTEGER,
    support_roles TEXT NOT NULL DEFAULT '[]',   -- json [int, ...]
    panel_title TEXT NOT NULL DEFAULT 'Support Tickets',
    panel_description TEXT NOT NULL DEFAULT 'Open a ticket to get help.',
    panel_emoji TEXT DEFAULT '\U0001f3e1',
    button_label TEXT NOT NULL DEFAULT 'Open a Ticket',
    max_open INTEGER NOT NULL DEFAULT 1,
    transcript_format TEXT NOT NULL DEFAULT 'html',  -- html | txt
    delete_on_close INTEGER NOT NULL DEFAULT 1,
    dm_on_close INTEGER NOT NULL DEFAULT 1
);

-- One row per ticket, kept after close so transcripts stay auditable.
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER UNIQUE,
    number INTEGER NOT NULL,
    opener_id INTEGER NOT NULL,
    staff_id INTEGER,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'open',   -- open | closed | deleted
    close_reason TEXT,
    closed_by INTEGER,
    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at INTEGER
);
CREATE INDEX IF NOT EXISTS tickets_guild_idx ON tickets (guild_id, status);
CREATE INDEX IF NOT EXISTS tickets_opener_idx ON tickets (guild_id, opener_id, status);

-- Monotonic per-guild ticket number used in channel names / transcripts.
CREATE TABLE IF NOT EXISTS ticket_counters (
    guild_id INTEGER PRIMARY KEY,
    counter INTEGER NOT NULL DEFAULT 0
);

-- Members blocked from opening tickets.
CREATE TABLE IF NOT EXISTS ticket_blacklist (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER,
    reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, user_id)
);
