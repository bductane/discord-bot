import asyncio
import logging
import os
import re
import string
import typing
from datetime import datetime, timedelta
from types import SimpleNamespace as param

import discord
import isodate
from discord.ext.commands import MissingRequiredArgument, CommandError

from core.time import human_timedelta
from core.utils import is_image_url, days, match_user_id
from core.utils import truncate, ignore, error

logger = logging.getLogger("Modmail")


class Thread:
    """Represents a discord Modmail thread"""

    def __init__(
        self,
        manager: "ThreadManager",
        recipient: typing.Union[discord.Member, discord.User, int],
        channel: typing.Union[discord.DMChannel, discord.TextChannel] = None,
    ):
        self.manager = manager
        self.bot = manager.bot
        if isinstance(recipient, int):
            self._id = recipient
            self._recipient = None
        else:
            if recipient.bot:
                raise CommandError("Recipient cannot be a bot.")
            self._id = recipient.id
            self._recipient = recipient
        self._channel = channel
        self.genesis_message = None
        self._ready_event = asyncio.Event()
        self.close_task = None
        self.auto_close_task = None

    def __repr__(self):
        return (
            f'Thread(recipient="{self.recipient or self.id}", '
            f"channel={self.channel.id})"
        )

    async def wait_until_ready(self) -> None:
        """Blocks execution until the thread is fully set up."""
        await self._ready_event.wait()

    @property
    def id(self) -> int:
        return self._id

    @property
    def channel(self) -> typing.Union[discord.TextChannel, discord.DMChannel]:
        return self._channel

    @property
    def recipient(self) -> typing.Optional[typing.Union[discord.User, discord.Member]]:
        return self._recipient

    @property
    def ready(self) -> bool:
        return self._ready_event.is_set()

    @ready.setter
    def ready(self, flag: bool):
        if flag:
            self._ready_event.set()
        else:
            self._ready_event.clear()

    async def setup(self, *, creator=None, category=None):
        """Create the thread channel and other io related initialisation tasks"""

        self.bot.dispatch("thread_create", self)

        recipient = self.recipient

        # in case it creates a channel outside of category
        overwrites = {
            self.bot.modmail_guild.default_role: discord.PermissionOverwrite(
                read_messages=False
            )
        }

        category = category or self.bot.main_category

        if category is not None:
            overwrites = None

        try:
            channel = await self.bot.modmail_guild.create_text_channel(
                name=self.manager.format_channel_name(recipient),
                category=category,
                overwrites=overwrites,
                reason="Creating a thread channel",
            )
        except discord.HTTPException as e:  # Failed to create due to 50 channel limit.
            del self.manager.cache[self.id]
            log_channel = self.bot.log_channel

            em = discord.Embed(color=discord.Color.red())
            em.title = "Error while trying to create a thread"
            em.description = e.message
            em.add_field(name="Recipient", value=recipient.mention)

            if log_channel is not None:
                return await log_channel.send(embed=em)

        self._channel = channel

        try:
            log_url, log_data = await asyncio.gather(
                self.bot.api.create_log_entry(recipient, channel, creator or recipient),
                self.bot.api.get_user_logs(recipient.id),
            )

            log_count = sum(1 for log in log_data if not log["open"])
        except:  # Something went wrong with database?
            log_url = log_count = None
            # ensure core functionality still works

        topic = f"User ID: {recipient.id}"
        if creator:
            mention = None
        else:
            mention = self.bot.config.get("mention", "@here")

        async def send_genesis_message():
            info_embed = self.manager.format_info_embed(
                recipient, log_url, log_count, discord.Color.green()
            )
            try:
                msg = await channel.send(mention, embed=info_embed)
                self.bot.loop.create_task(msg.pin())
                self.genesis_message = msg
            except Exception as e:
                pass
            finally:
                self.ready = True
                self.bot.dispatch("thread_ready", self)

        await channel.edit(topic=topic)
        self.bot.loop.create_task(send_genesis_message())

        # Once thread is ready, tell the recipient.
        thread_creation_response = self.bot.config.get(
            "thread_creation_response",
            "Un membre du staff ESNC vous contactera dès que possible.",
        )

        embed = discord.Embed(
            color=self.bot.mod_color,
            description=thread_creation_response,
            timestamp=channel.created_at,
        )

        footer = "Your message has been sent"
        if not self.bot.config.get("disable_recipient_thread_close"):
            footer = "Cliquez sur le cadenas pour fermer le ticket"

        footer = self.bot.config.get("thread_creation_footer", footer)
        embed.set_footer(text=footer, icon_url=self.bot.guild.icon_url)
        embed.title = self.bot.config.get("thread_creation_title", "Ticket créé")

        if creator is None:
            msg = await recipient.send(embed=embed)
            if not self.bot.config.get("disable_recipient_thread_close"):
                close_emoji = self.bot.config.get("close_emoji", "🔒")
                close_emoji = await self.bot.convert_emoji(close_emoji)
                await msg.add_reaction(close_emoji)

    def _close_after(self, closer, silent, delete_channel, message):
        return self.bot.loop.create_task(
            self._close(closer, silent, delete_channel, message, True)
        )

    async def close(
        self,
        *,
        closer: typing.Union[discord.Member, discord.User],
        after: int = 0,
        silent: bool = False,
        delete_channel: bool = True,
        message: str = None,
        auto_close: bool = False,
    ) -> None:
        """Close a thread now or after a set time in seconds"""

        # restarts the after timer
        await self.cancel_closure(auto_close)

        if after > 0:
            # TODO: Add somewhere to clean up broken closures
            #  (when channel is already deleted)
            await self.bot.config.update()
            now = datetime.utcnow()
            items = {
                # 'initiation_time': now.isoformat(),
                "time": (now + timedelta(seconds=after)).isoformat(),
                "closer_id": closer.id,
                "silent": silent,
                "delete_channel": delete_channel,
                "message": message,
                "auto_close": auto_close,
            }
            self.bot.config.closures[str(self.id)] = items
            await self.bot.config.update()

            task = self.bot.loop.call_later(
                after, self._close_after, closer, silent, delete_channel, message
            )

            if auto_close:
                self.auto_close_task = task
            else:
                self.close_task = task
        else:
            await self._close(closer, silent, delete_channel, message)

    async def _close(
        self, closer, silent=False, delete_channel=True, message=None, scheduled=False
    ):
        del self.manager.cache[self.id]

        await self.cancel_closure(all=True)

        # Cancel auto closing the thread if closed by any means.

        if str(self.id) in self.bot.config.subscriptions:
            del self.bot.config.subscriptions[str(self.id)]

        # Logging
        log_data = await self.bot.api.post_log(
            self.channel.id,
            {
                "open": False,
                "closed_at": str(datetime.utcnow()),
                "close_message": message if not silent else None,
                "closer": {
                    "id": str(closer.id),
                    "name": closer.name,
                    "discriminator": closer.discriminator,
                    "avatar_url": str(closer.avatar_url),
                    "mod": True,
                },
            },
        )

        if log_data is not None and isinstance(log_data, dict):
            prefix = os.getenv("LOG_URL_PREFIX", "/logs")
            if prefix == "NONE":
                prefix = ""
            log_url = f"{self.bot.config.log_url.strip('/')}{prefix}/{log_data['key']}"

            if log_data["messages"]:
                content = str(log_data["messages"][0]["content"])
                sneak_peak = content.replace("\n", "")
            else:
                sneak_peak = "No content"

            desc = f"[`{log_data['key']}`]({log_url}): "
            desc += truncate(sneak_peak, max=75 - 13)
        else:
            desc = "Could not resolve log url."
            log_url = None

        embed = discord.Embed(description=desc, color=discord.Color.red())

        if self.recipient is not None:
            user = f"{self.recipient} (`{self.id}`)"
        else:
            user = f"`{self.id}`"

        if self.id == closer.id:
            _closer = "the Recipient"
        else:
            _closer = f"{closer} ({closer.id})"

        embed.title = user

        event = "Thread Closed as Scheduled" if scheduled else "Ticket fermé"
        # embed.set_author(name=f'Event: {event}', url=log_url)
        embed.set_footer(text=f"{event} by {_closer}")
        embed.timestamp = datetime.utcnow()

        tasks = [self.bot.config.update()]

        try:
            tasks.append(self.bot.log_channel.send(embed=embed))
        except (ValueError, AttributeError):
            pass

        # Thread closed message

        embed = discord.Embed(
            title=self.bot.config.get("thread_close_title", "Thread Closed"),
            color=discord.Color.red(),
            timestamp=datetime.utcnow(),
        )

        if not message:
            if self.id == closer.id:
                message = self.bot.config.get(
                    "thread_self_close_response", "Vous avez fermé ce ticket."
                )
            else:
                message = self.bot.config.get(
                    "thread_close_response",
                    "{closer.mention} has closed this Modmail thread.",
                )

        message = message.format(closer=closer, loglink=log_url, logkey=log_data["key"])

        embed.description = message
        footer = self.bot.config.get(
            "thread_close_footer", "Répondre va créer un nouveau ticket !"
        )
        embed.set_footer(text=footer, icon_url=self.bot.guild.icon_url)

        if not silent and self.recipient is not None:
            tasks.append(self.recipient.send(embed=embed))

        if delete_channel:
            tasks.append(self.channel.delete())

        await asyncio.gather(*tasks)

    async def cancel_closure(self, auto_close: bool = False, all: bool = False) -> None:
        if self.close_task is not None and (not auto_close or all):
            self.close_task.cancel()
            self.close_task = None
        if self.auto_close_task is not None and (auto_close or all):
            self.auto_close_task.cancel()
            self.auto_close_task = None

        to_update = self.bot.config.closures.pop(str(self.id), None)
        if to_update is not None:
            await self.bot.config.update()

    @staticmethod
    async def _find_thread_message(channel, message_id):
        async for msg in channel.history():
            if not msg.embeds:
                continue
            embed = msg.embeds[0]
            if embed and embed.author and embed.author.url:
                if str(message_id) == str(embed.author.url).split("/")[-1]:
                    return msg

    async def _fetch_timeout(
        self
    ) -> typing.Union[None, isodate.duration.Duration, timedelta]:
        """
        This grabs the timeout value for closing threads automatically
        from the ConfigManager and parses it for use internally.

        :returns: None if no timeout is set.
        """
        timeout = self.bot.config.get("thread_auto_close")
        if timeout is None:
            return timeout
        else:
            try:
                timeout = isodate.parse_duration(timeout)
            except isodate.ISO8601Error:
                logger.warning(
                    "The auto_close_thread limit needs to be a "
                    "ISO-8601 duration formatted duration string "
                    'greater than 0 days, not "%s".',
                    str(timeout),
                )
                del self.bot.config.cache["thread_auto_close"]
                await self.bot.config.update()
                timeout = None
        return timeout

    async def _restart_close_timer(self):
        """
        This will create or restart a timer to automatically close this
        thread.
        """
        timeout = await self._fetch_timeout()

        # Exit if timeout was not set
        if timeout is None:
            return

        # Set timeout seconds
        seconds = timeout.total_seconds()
        # seconds = 20  # Uncomment to debug with just 20 seconds
        reset_time = datetime.utcnow() + timedelta(seconds=seconds)
        human_time = human_timedelta(dt=reset_time)

        # Grab message
        close_message = self.bot.config.get(
            "thread_auto_close_response",
            f"This thread has been closed automatically due to inactivity "
            f"after {human_time}.",
        )
        time_marker_regex = "%t"
        if len(re.findall(time_marker_regex, close_message)) == 1:
            close_message = re.sub(time_marker_regex, str(human_time), close_message)
        elif len(re.findall(time_marker_regex, close_message)) > 1:
            logger.warning(
                "The thread_auto_close_response should only contain one"
                f" '{time_marker_regex}' to specify time."
            )

        await self.close(
            closer=self.bot.user, after=seconds, message=close_message, auto_close=True
        )

    async def edit_message(self, message_id: int, message: str) -> None:
        recipient_msg, channel_msg = await asyncio.gather(
            self._find_thread_message(self.recipient, message_id),
            self._find_thread_message(self.channel, message_id),
        )

        channel_embed = channel_msg.embeds[0]
        channel_embed.description = message

        tasks = [channel_msg.edit(embed=channel_embed)]

        if recipient_msg:
            recipient_embed = recipient_msg.embeds[0]
            recipient_embed.description = message
            tasks.append(recipient_msg.edit(embed=recipient_embed))

        await asyncio.gather(*tasks)

    async def delete_message(self, message_id):
        msg_recipient, msg_channel = await asyncio.gather(
            self._find_thread_message(self.recipient, message_id),
            self._find_thread_message(self.channel, message_id),
        )
        await asyncio.gather(msg_recipient.delete(), msg_channel.delete())

    async def note(self, message: discord.Message) -> None:
        if not message.content and not message.attachments:
            raise MissingRequiredArgument(param(name="msg"))

        _, msg = await asyncio.gather(
            self.bot.api.append_log(message, self.channel.id, type_="system"),
            self.send(message, self.channel, note=True),
        )

        return msg

    async def reply(self, message: discord.Message, anonymous: bool = False) -> None:
        if not message.content and not message.attachments:
            raise MissingRequiredArgument(param(name="msg"))
        if all(not g.get_member(self.id) for g in self.bot.guilds):
            return await message.channel.send(
                embed=discord.Embed(
                    color=discord.Color.red(),
                    description="Your message could not be delivered since "
                    "the recipient shares no servers with the bot.",
                )
            )

        tasks = []

        try:
            await self.send(
                message, destination=self.recipient, from_mod=True, anonymous=anonymous
            )
        except Exception:
            logger.info(error("Message delivery failed:"), exc_info=True)
            tasks.append(
                message.channel.send(
                    embed=discord.Embed(
                        color=discord.Color.red(),
                        description="Your message could not be delivered as "
                        "the recipient is only accepting direct "
                        "messages from friends, or the bot was "
                        "blocked by the recipient.",
                    )
                )
            )
        else:
            # Send the same thing in the thread channel.
            tasks.append(
                self.send(
                    message,
                    destination=self.channel,
                    from_mod=True,
                    anonymous=anonymous,
                )
            )

            tasks.append(
                self.bot.api.append_log(
                    message,
                    self.channel.id,
                    type_="anonymous" if anonymous else "thread_message",
                )
            )

            # Cancel closing if a thread message is sent.
            if self.close_task is not None:
                await self.cancel_closure()
                tasks.append(
                    self.channel.send(
                        embed=discord.Embed(
                            color=discord.Color.red(),
                            description="Scheduled close has been cancelled.",
                        )
                    )
                )

        await asyncio.gather(*tasks)

    async def send(
        self,
        message: discord.Message,
        destination: typing.Union[
            discord.TextChannel, discord.DMChannel, discord.User, discord.Member
        ] = None,
        from_mod: bool = False,
        note: bool = False,
        anonymous: bool = False,
    ) -> None:

        self.bot.loop.create_task(
            self._restart_close_timer()
        )  # Start or restart thread auto close

        if self.close_task is not None:
            # cancel closing if a thread message is sent.
            self.bot.loop.create_task(self.cancel_closure())
            self.bot.loop.create_task(
                self.channel.send(
                    embed=discord.Embed(
                        color=discord.Color.red(),
                        description="Scheduled close has been cancelled.",
                    )
                )
            )

        if not self.ready:
            await self.wait_until_ready()

        if not from_mod and not note:
            self.bot.loop.create_task(self.bot.api.append_log(message, self.channel.id))

        destination = destination or self.channel

        author = message.author

        embed = discord.Embed(description=message.content, timestamp=message.created_at)

        system_avatar_url = (
            "https://discordapp.com/assets/f78426a064bc9dd24847519259bc42af.png"
        )

        if not note:
            if (
                anonymous
                and from_mod
                and not isinstance(destination, discord.TextChannel)
            ):
                # Anonymously sending to the user.
                tag = self.bot.config.get("mod_tag", str(message.author.top_role))
                name = self.bot.config.get("anon_username", tag)
                avatar_url = self.bot.config.get(
                    "anon_avatar_url", self.bot.guild.icon_url
                )
            else:
                # Normal message
                name = str(author)
                avatar_url = author.avatar_url

            embed.set_author(name=name, icon_url=avatar_url, url=message.jump_url)
        else:
            # Special note messages
            embed.set_author(
                name=f"Note ({author.name})",
                icon_url=system_avatar_url,
                url=message.jump_url,
            )

        delete_message = not bool(message.attachments)

        attachments = [(a.url, a.filename) for a in message.attachments]

        images = [x for x in attachments if is_image_url(*x)]
        attachments = [x for x in attachments if not is_image_url(*x)]

        image_links = [
            (link, None)
            for link in re.findall(
                r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+",
                message.content,
            )
        ]

        image_links = [x for x in image_links if is_image_url(*x)]
        images.extend(image_links)

        embedded_image = False

        prioritize_uploads = any(i[1] is not None for i in images)

        additional_images = []
        additional_count = 1

        for att in images:
            if not prioritize_uploads or (
                is_image_url(*att) and not embedded_image and att[1]
            ):
                embed.set_image(url=att[0])
                if att[1]:
                    embed.add_field(name="Image", value=f"[{att[1]}]({att[0]})")
                embedded_image = True
            elif att[1] is not None:
                if note:
                    color = discord.Color.blurple()
                elif from_mod:
                    color = self.bot.mod_color
                else:
                    color = self.bot.recipient_color

                img_embed = discord.Embed(color=color)
                img_embed.set_image(url=att[0])
                img_embed.title = att[1]
                img_embed.url = att[0]
                img_embed.set_footer(
                    text=f"Additional Image Upload ({additional_count})"
                )
                img_embed.timestamp = message.created_at
                additional_images.append(destination.send(embed=img_embed))
                additional_count += 1

        file_upload_count = 1

        for att in attachments:
            embed.add_field(
                name=f"File upload ({file_upload_count})", value=f"[{att[1]}]({att[0]})"
            )
            file_upload_count += 1

        if from_mod:
            # noinspection PyUnresolvedReferences,PyDunderSlots
            embed.color = self.bot.mod_color  # pylint: disable=E0237
            # Anonymous reply sent in thread channel
            if anonymous and isinstance(destination, discord.TextChannel):
                embed.set_footer(text="Anonymous Reply")
            # Normal messages
            elif not anonymous:
                tag = self.bot.config.get("mod_tag", str(message.author.top_role))
                embed.set_footer(text=tag)  # Normal messages
            else:
                embed.set_footer(text=self.bot.config.get("anon_tag", "Response"))
        elif note:
            # noinspection PyUnresolvedReferences,PyDunderSlots
            embed.color = discord.Color.blurple()  # pylint: disable=E0237
        else:
            embed.set_footer(text=f"Recipient")
            # noinspection PyUnresolvedReferences,PyDunderSlots
            embed.color = self.bot.recipient_color  # pylint: disable=E0237

        await destination.trigger_typing()

        if not from_mod and not note:
            mentions = self.get_notifications()
        else:
            mentions = None

        _msg = await destination.send(mentions, embed=embed)

        if additional_images:
            self.ready = False
            await asyncio.gather(*additional_images)
            self.ready = True

        if delete_message:
            self.bot.loop.create_task(ignore(message.delete()))

        return _msg

    def get_notifications(self) -> str:
        config = self.bot.config
        key = str(self.id)

        mentions = []
        mentions.extend(config["subscriptions"].get(key, []))

        if key in config["notification_squad"]:
            mentions.extend(config["notification_squad"][key])
            del config["notification_squad"][key]
            self.bot.loop.create_task(config.update())

        return " ".join(mentions)


class ThreadManager:
    """Class that handles storing, finding and creating Modmail threads."""

    def __init__(self, bot):
        self.bot = bot
        self.cache = {}

    async def populate_cache(self) -> None:
        for channel in self.bot.modmail_guild.text_channels:
            if (
                channel.category != self.bot.main_category
                and not self.bot.using_multiple_server_setup
            ):
                continue
            await self.find(channel=channel)

    def __len__(self):
        return len(self.cache)

    def __iter__(self):
        return iter(self.cache.values())

    def __getitem__(self, item: str) -> Thread:
        return self.cache[item]

    async def find(
        self,
        *,
        recipient: typing.Union[discord.Member, discord.User] = None,
        channel: discord.TextChannel = None,
        recipient_id: int = None,
    ) -> Thread:
        """Finds a thread from cache or from discord channel topics."""
        if recipient is None and channel is not None:
            return await self._find_from_channel(channel)

        thread = None

        if recipient:
            recipient_id = recipient.id

        try:
            thread = self.cache[recipient_id]
            if not thread.channel or not self.bot.get_channel(thread.channel.id):
                self.bot.loop.create_task(
                    thread.close(
                        closer=self.bot.user, silent=True, delete_channel=False
                    )
                )
                thread = None
        except KeyError:
            channel = discord.utils.get(
                self.bot.modmail_guild.text_channels, topic=f"User ID: {recipient_id}"
            )
            if channel:
                thread = Thread(self, recipient or recipient_id, channel)
                self.cache[recipient_id] = thread
                thread.ready = True
        return thread

    async def _find_from_channel(self, channel):
        """
        Tries to find a thread from a channel channel topic,
        if channel topic doesnt exist for some reason, falls back to
        searching channel history for genesis embed and
        extracts user_id from that.
        """
        user_id = -1

        if channel.topic:
            user_id = match_user_id(channel.topic)

        # BUG: When discord fails to create channel topic.
        # search through message history
        elif channel.topic is None:
            try:
                async for message in channel.history(limit=100):
                    if message.author != self.bot.user:
                        continue
                    if message.embeds:
                        embed = message.embeds[0]
                        if embed.footer.text:
                            user_id = match_user_id(embed.footer.text)
                            if user_id != -1:
                                break
            except discord.NotFound:
                # When the channel's deleted.
                pass

        if user_id != -1:
            if user_id in self.cache:
                return self.cache[user_id]

            recipient = self.bot.get_user(user_id)
            if recipient is None:
                self.cache[user_id] = thread = Thread(self, user_id, channel)
            else:
                self.cache[user_id] = thread = Thread(self, recipient, channel)
            thread.ready = True

            return thread

    def create(
        self,
        recipient: typing.Union[discord.Member, discord.User],
        *,
        creator: typing.Union[discord.Member, discord.User] = None,
        category: discord.CategoryChannel = None,
    ) -> Thread:
        """Creates a Modmail thread"""
        # create thread immediately so messages can be processed
        thread = Thread(self, recipient)
        self.cache[recipient.id] = thread

        # Schedule thread setup for later
        self.bot.loop.create_task(thread.setup(creator=creator, category=category))
        return thread

    async def find_or_create(self, recipient) -> Thread:
        return await self.find(recipient=recipient) or self.create(recipient)

    def format_channel_name(self, author):
        """Sanitises a username for use with text channel names"""
        name = author.name.lower()
        new_name = (
            "".join(l for l in name if l not in string.punctuation and l.isprintable())
            or "null"
        )
        new_name += f"-{author.discriminator}"

        while new_name in [c.name for c in self.bot.modmail_guild.text_channels]:
            new_name += "-x"  # two channels with same name

        return new_name

    def format_info_embed(self, user, log_url, log_count, color):
        """Get information about a member of a server
        supports users from the guild or not."""
        member = self.bot.guild.get_member(user.id)
        time = datetime.utcnow()

        # key = log_url.split('/')[-1]

        role_names = ""
        if member:
            sep_server = self.bot.using_multiple_server_setup
            separator = ", " if sep_server else " "

            roles = []

            for role in sorted(member.roles, key=lambda r: r.position):
                if role.name == "@everyone":
                    continue

                fmt = role.name if sep_server else role.mention
                roles.append(fmt)

                if len(separator.join(roles)) > 1024:
                    roles.append("...")
                    while len(separator.join(roles)) > 1024:
                        roles.pop(-2)
                    break

            role_names = separator.join(roles)

        embed = discord.Embed(color=color, description=user.mention, timestamp=time)

        created = str((time - user.created_at).days)
        # if not role_names:
        #     embed.add_field(name='Mention', value=user.mention)
        # embed.add_field(name='Registered', value=created + days(created))
        embed.description += f" was created {days(created)}"

        footer = "User ID: " + str(user.id)
        embed.set_footer(text=footer)
        embed.set_author(name=str(user), icon_url=user.avatar_url, url=log_url)
        # embed.set_thumbnail(url=avi)

        if member:
            joined = str((time - member.joined_at).days)
            # embed.add_field(name='Joined', value=joined + days(joined))
            embed.description += f", joined {days(joined)}"

            if member.nick:
                embed.add_field(name="Nickname", value=member.nick, inline=True)
            if role_names:
                embed.add_field(name="Roles", value=role_names, inline=True)
        else:
            embed.set_footer(text=f"{footer} • (not in main server)")

        if log_count:
            # embed.add_field(name='Past logs', value=f'{log_count}')
            thread = "thread" if log_count == 1 else "threads"
            embed.description += f" with **{log_count}** past {thread}."
        else:
            embed.description += "."

        mutual_guilds = [g for g in self.bot.guilds if user in g.members]
        if user not in self.bot.guild.members or len(mutual_guilds) > 1:
            embed.add_field(
                name="Mutual Servers", value=", ".join(g.name for g in mutual_guilds)
            )

        return embed
