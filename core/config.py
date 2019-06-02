import asyncio
import copy
import emoji
import inspect
import json
import logging
import os
import typing
from distutils.util import strtobool
from types import SimpleNamespace

import isodate

import discord
from discord.ext import commands

from core._color_data import ALL_COLORS
from core.models import InvalidConfigError
from core.time import UserFriendlyTime
from core.utils import error, info


logger = logging.getLogger("Modmail")


class ConfigConverter:
    def __init__(self, default: typing.Any = None):
        self.default = default

    async def convert(self, manager: 'ConfigManager', key: str, argument: typing.Any) -> typing.Optional[typing.Any]:
        """
        Convert a configuration value to their respective types.
        """
        raise NotImplementedError('Derived classes need to implement this.')

    async def sanitize(self, manager: 'ConfigManager', key: str, argument: typing.Any) -> typing.Optional[typing.Any]:
        """
        Convert a string configuration value into standardized configuration values.
        """
        raise NotImplementedError('Derived classes need to implement this.')


class BooleanConverter(ConfigConverter):
    def __init__(self, default: bool = False):
        super().__init__(default)

    async def convert(self, manager, key, argument) -> typing.Optional[bool]:
        if argument is None:
            return self.default

        if argument in {0, 1}:
            return bool(argument)
        try:
            return strtobool(argument)
        except ValueError:
            logger.error(error('"%s" is not a valid yes/no choice for configuration "%s", '
                               'it has been removed.'), argument, key)
            manager.remove(key)
        return self.default

    async def sanitize(self, manager, key, argument) -> typing.Optional[int]:
        if argument is None:
            return None

        try:
            # Converts to 0/1
            return int(strtobool(argument))
        except ValueError:
            raise InvalidConfigError(f'"{argument}" is not a valid yes/no value.')


class StrConverter(ConfigConverter):
    async def convert(self, manager, key, argument) -> typing.Optional[str]:
        if argument is None:
            return self.default

        return str(argument)

    async def sanitize(self, manager, key, argument) -> typing.Optional[str]:
        if argument is None:
            return None

        return str(argument)


class IntConverter(ConfigConverter):
    async def convert(self, manager, key, argument) -> typing.Optional[int]:
        if argument is None:
            return self.default

        try:
            return int(argument)
        except ValueError:
            logger.error(error('"%s" is not a valid integer for configuration "%s", '
                               'it has been removed.'), argument, key)
            manager.remove(key)
        return self.default

    async def sanitize(self, manager, key, argument) -> typing.Optional[int]:
        if argument is None:
            return None

        try:
            return int(argument)
        except ValueError:
            raise InvalidConfigError('Invalid integer value.')


class ColorConverter(ConfigConverter):
    async def convert(self, manager, key, argument) -> typing.Optional[int]:
        if argument is None:
            return self.default

        try:
            return int(argument.lstrip("#"), base=16)
        except ValueError:
            logger.error(error('"%s" is not a valid hex color for configuration "%s", '
                               'it has been removed.'), argument, key)
            manager.remove(key)
        return self.default

    async def sanitize(self, manager, key, argument) -> typing.Optional[str]:
        if argument is None:
            return None

        hex_ = ALL_COLORS.get(argument)

        if hex_ is not None:
            return hex_

        hex_ = str(argument)
        if hex_.startswith("#"):
            hex_ = hex_[1:]
        if len(hex_) == 3:
            hex_ = ''.join(s for s in hex_ for _ in range(2))
        if len(hex_) != 6:
            raise InvalidConfigError("Invalid color name or hex.")
        try:
            int(hex_)
        except ValueError:
            raise InvalidConfigError("Invalid color name or hex.")
        return "#" + hex_


class TimeDeltaConverter(ConfigConverter):
    def __init__(self, default: isodate.duration.Duration = None):
        super().__init__(default if default is not None else isodate.duration.Duration())

    async def convert(self, manager, key, argument) -> typing.Optional[isodate.duration.Duration]:
        if argument is None:
            return self.default
        try:
            return isodate.parse_duration(argument)
        except isodate.ISO8601Error:
            logger.error(error('"%s" is not a valid ISO8601 duration format string for configuration "%s", '
                               'it has been removed.'), argument, key)
            manager.remove(key)
        return self.default

    async def sanitize(self, manager, key, argument) -> typing.Optional[str]:
        if argument is None:
            return None
        try:
            return isodate.parse_duration(argument)
        except isodate.ISO8601Error:
            try:
                converter = UserFriendlyTime()
                time = await converter.convert(None, argument)
                if time.arg:
                    raise InvalidConfigError(
                        "Unrecognized time, please use ISO-8601 duration format "
                        'string or a simpler "human readable" time.'
                    )
            except commands.BadArgument as exc:
                raise InvalidConfigError(*exc.args)
            except Exception:
                raise InvalidConfigError(
                    "Unrecognized time, please use ISO-8601 duration format "
                    'string or a simpler "human readable" time.'
                )
            return isodate.duration_isoformat(time.dt - converter.now)


class EmojiConverter(ConfigConverter):
    async def convert(self, manager, key, argument) -> typing.Optional[typing.Union[str, discord.Emoji]]:
        if argument is None:
            return self.default

        if argument in emoji.UNICODE_EMOJI:
            return argument
        try:
            converter = commands.EmojiConverter()
            ctx = SimpleNamespace(bot=manager.bot, guild=manager.bot.guild)
            return await converter.convert(ctx, argument.strip(":"))
        except commands.BadArgument:
            logger.error(error('"%s" is not a valid emoji  for configuration "%s", it has been removed.'),
                         argument, key)
            manager.remove(key)
        return self.default

    async def sanitize(self, manager, key, argument) -> typing.Optional[str]:
        if argument is None:
            return None

        if argument in emoji.UNICODE_EMOJI:
            return argument

        try:
            converter = commands.EmojiConverter()
            ctx = SimpleNamespace(bot=manager.bot, guild=manager.bot.guild)
            return await converter.convert(ctx, argument.strip(":"))
        except commands.BadArgument as exc:
            raise InvalidConfigError(*exc.args)


def _check_(arg):
    if inspect.isclass(arg):
        return issubclass(arg, ConfigConverter)
    return isinstance(arg, ConfigConverter)


class ArrayWithDefaults(ConfigConverter):
    def __init__(self, default: list = None):
        super().__init__(default if default is not None else [])
        self.type_ = None

    def __getitem__(self, type_: typing.Type[ConfigConverter]):
        self = copy.deepcopy(self)
        self.type_ = type_

        if inspect.isclass(self.type_):
            self.type_ = self.type_()

        if not isinstance(self.type_, ConfigConverter) and self.type_ is not None:
            raise ValueError("Must derive from ConfigConverter.")
        return self

    async def convert(self, manager, key, argument) -> typing.Optional[list]:
        if argument is None:
            return self.default

        try:
            return [self.type_.convert(manager, key, item)
                    if self.type_ is not None else item for item in list(argument)]
        except TypeError:
            logger.error(error(
                '"%s" is not a valid array for configuration "%s", it has been removed.'),
                         argument, key)
            manager.remove(key)
        return self.default

    async def sanitize(self, manager, key, argument) -> typing.Optional[list]:
        if argument is None:
            return None

        try:
            return [self.type_.sanitize(manager, key, item)
                    if self.type_ is not None else item for item in list(argument)]
        except TypeError:
            raise InvalidConfigError(f'"{key}" must be an array.')


Array = ArrayWithDefaults()


class MappingWithDefaults(ConfigConverter):
    def __init__(self, default: dict = None):
        super().__init__(default if default is not None else {})
        self.key_type = None
        self.arg_type = None

    def __getitem__(self, type_: typing.Union[typing.Tuple[typing.Type[ConfigConverter],
                                                           typing.Type[ConfigConverter]],
                                              typing.Type[ConfigConverter]]):
        self = copy.deepcopy(self)
        if isinstance(type_, tuple):
            self.key_type, self.arg_type = type_
        else:
            self.key_type = StrConverter
            self.arg_type = type_

        if inspect.isclass(self.key_type):
            self.key_type = self.key_type()
        if inspect.isclass(self.arg_type):
            self.arg_type = self.arg_type()

        if not isinstance(self.key_type, ConfigConverter) or (not isinstance(self.arg_type, ConfigConverter) and
                                                              self.arg_type is not None):
            raise ValueError("Must derive from ConfigConverter.")
        return self

    async def convert(self, manager, key, argument) -> typing.Optional[dict]:
        if argument is None:
            return self.default

        try:
            return {self.key_type.convert(manager, key, name): self.arg_type.convert(manager, key, item)
                    if self.arg_type is not None else item for name, item in dict(argument)}
        except TypeError:
            logger.error(error(
                '"%s" is not a valid mapping for configuration "%s", it has been removed.'),
                         argument, key)
            manager.remove(key)
        return self.default

    async def sanitize(self, manager, key, argument) -> typing.Optional[dict]:
        if argument is None:
            return None

        try:
            return {self.key_type.sanitize(manager, key, name): self.arg_type.sanitize(manager, key, item)
                    if self.arg_type is not None else item for name, item in dict(argument)}
        except TypeError:
            raise InvalidConfigError(f'"{key}" must be a mapping.')


Mapping = MappingWithDefaults()


class ConfigManager:
    public_keys = {
        # activity
        "twitch_url": StrConverter("https://www.twitch.tv/discord-modmail/"),
        # bot settings
        "main_category_id": IntConverter,
        "disable_autoupdates": BooleanConverter(False),
        "prefix": StrConverter('?'),
        "mention": StrConverter('@here'),
        "main_color": ColorConverter(discord.Color.blurple()),
        "user_typing": BooleanConverter(False),
        "mod_typing": BooleanConverter(False),
        "account_age": TimeDeltaConverter,
        "guild_age": TimeDeltaConverter,
        "reply_without_command": BooleanConverter(False),
        # logging
        "log_channel_id": IntConverter,
        # threads
        "sent_emoji": EmojiConverter('✅'),
        "blocked_emoji": EmojiConverter('🚫'),
        "close_emoji": EmojiConverter('🔒'),
        "disable_recipient_thread_close": BooleanConverter(False),
        "thread_creation_response": StrConverter('The staff team will get back to you as soon as possible.'),
        "thread_creation_footer": StrConverter,
        "thread_creation_title": StrConverter("Thread Created"),
        "thread_close_footer": StrConverter("Replying will create a new thread"),
        "thread_close_title": StrConverter("Thread Closed"),
        "thread_close_response": StrConverter("{closer.mention} has closed this Modmail thread."),
        "thread_self_close_response": StrConverter("You have closed this Modmail thread."),
        # moderation
        "recipient_color": ColorConverter(discord.Color.gold()),
        "mod_tag": StrConverter,
        "mod_color": ColorConverter(discord.Color.green()),
        # anonymous message
        "anon_username": StrConverter,
        "anon_avatar_url": StrConverter,
        "anon_tag": StrConverter("Response"),
    }

    private_keys = {
        # bot presence
        "activity_message": StrConverter,
        "activity_type": StrConverter,
        "status": StrConverter,
        "oauth_whitelist": Array[IntConverter],
        # moderation
        "blocked": Mapping[IntConverter, StrConverter],
        "command_permissions": Mapping[Array[IntConverter]],
        "level_permissions": Mapping[Array[IntConverter]],
        # threads
        "snippets": Mapping[StrConverter],
        "notification_squad": Mapping[IntConverter, Array[StrConverter]],
        "subscriptions": Mapping[IntConverter, Array[StrConverter]],
        "closures": Mapping[IntConverter, Mapping[StrConverter]],
        # misc
        "aliases": Mapping[StrConverter],
        "plugins": Array[StrConverter],
    }

    protected_keys = {
        # Modmail
        "modmail_guild_id": IntConverter,
        "guild_id": IntConverter,
        "log_url": StrConverter,
        "mongo_uri": StrConverter,
        "owners": StrConverter,
        # bot
        "token": StrConverter,
        # GitHub
        "github_access_token": StrConverter,
        # Logging
        "log_level": StrConverter('INFO'),
    }

    all_keys = {**public_keys, **private_keys, **protected_keys}

    def __init__(self, bot):
        self.bot = bot
        self._raw_cache = {}
        self._ready_event = asyncio.Event()

    async def load_cache(self) -> None:
        logger.debug(info('Recreating local config cache.'))
        self._raw_cache = {}
        await self.refresh()
        if os.path.exists("config.json"):
            with open("config.json") as f:
                # Config json should override env vars
                for k, v in json.load(f).items():
                    await self.set(k, v, allow_all=True)
        self._ready_event.set()

    async def wait_until_ready(self) -> None:
        logger.debug(info('Waiting for "_ready_event" to be set.'))
        await self._ready_event.wait()
        logger.debug(info('"_ready_event" has been set.'))

    def __repr__(self):
        return repr(self._raw_cache)

    async def update(self) -> None:
        """Updates the config with data from the cache"""
        logger.debug(info('Attempting to update configs.'))
        await self.bot.api.update_config(self._raw_cache)

    async def refresh(self) -> None:
        """Refreshes internal cache with data from database"""
        logger.debug(info('Refreshing configs from database.'))
        data = await self.bot.api.get_config()
        for k, v in data.items():
            await self.set(k, v, allow_all=True)

    async def set(self, key: str, item: typing.Any, *, allow_all=False) -> None:
        logger.debug(info('Attempting to set config "%s" to local cache.'), key)
        if key not in self.all_keys:
            raise InvalidConfigError(f'Configuration "{key}" is invalid.')
        if not allow_all:
            if key not in self.public_keys:
                raise InvalidConfigError(f'Attempting to set unsettable configuration "{key}".')

        converter = self.all_keys[key]
        if inspect.isclass(converter):
            converter = converter()

        self._raw_cache[key] = await converter.sanitize(self, key, item)
        logger.debug(info('Updated config "%s" in local cache.'), key)

    async def get(self, key: str, *, default=None, allow_all=False) -> typing.Any:
        logger.debug(info('Attempting to retrieve config "%s" from local cache.'), key)
        if key not in self.all_keys:
            raise InvalidConfigError(f'Configuration "{key}" is invalid.')
        if not allow_all:
            if key not in self.public_keys:
                raise InvalidConfigError(f'Attempting to set gettable configuration "{key}".')

        converter = self.all_keys[key]
        if inspect.isclass(converter):
            converter = converter(default)

        return await converter.convert(self, key, self._raw_cache.get(key, default))

    def remove(self, key: str, *, allow_all=False) -> None:
        logger.debug(info('Attempting to remove config "%s" from local cache.'), key)
        if key not in self.all_keys:
            raise InvalidConfigError(f'Configuration "{key}" is invalid.')
        if not allow_all:
            if key not in self.public_keys:
                raise InvalidConfigError(f'Attempting to set removable configuration "{key}".')

        if key in self._raw_cache:
            del self._raw_cache[key]
        logger.debug(info('Successfully removed "%s" from local cache.'), key)
