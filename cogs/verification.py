import logging
from datetime import datetime
from io import BytesIO

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)

# ── Static custom_ids used across classes ─────────────────────────
VERIFY_BUTTON_ID = "verification_button"


# ══════════════════════════════════════════════════════════════════
#  Persistent View — Verify Me button
# ══════════════════════════════════════════════════════════════════

class VerificationView(discord.ui.View):
    """Persistent view containing the Verify Me button."""

    def __init__(self, cog: "VerificationCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="✅ Verify Me",
        style=discord.ButtonStyle.green,
        custom_id=VERIFY_BUTTON_ID,
    )
    async def verify_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        """Handle the verify button click."""
        db = self.cog.db
        user = interaction.user
        guild = interaction.guild

        if guild is None:
            return

        # 1. Already verified?
        if await db.is_user_verified(user.id, guild.id):
            embed = discord.Embed(
                description="✅ You're already verified!",
                color=discord.Color.green(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # 2. Rate limit: max 3 attempts per hour
        recent = await db.get_recent_attempt_count(user.id, guild.id, hours=1)
        if recent >= 3:
            embed = discord.Embed(
                description="⏱️ You've reached the maximum verification attempts. Please try again in an hour.",
                color=discord.Color.orange(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # 3. Open modal
        modal = VerificationModal(self.cog)
        await interaction.response.send_modal(modal)


# ══════════════════════════════════════════════════════════════════
#  Verification Modal
# ══════════════════════════════════════════════════════════════════

class VerificationModal(discord.ui.Modal, title="🔞 Age Verification"):
    """Modal with three verification questions."""

    fave_waifu = discord.ui.TextInput(
        label="Your favourite anime waifu or actress 👀",
        placeholder="e.g. Zero Two, Marin Kitagawa...",
        style=discord.TextStyle.short,
        min_length=2,
        max_length=100,
        custom_id="fave_waifu",
    )
    age = discord.ui.TextInput(
        label="Your age",
        placeholder="Must be 18 or older to proceed",
        style=discord.TextStyle.short,
        min_length=1,
        max_length=3,
        custom_id="age",
    )
    server_intention = discord.ui.TextInput(
        label="What do you want from this server?",
        placeholder="Be honest, we don't judge 👀",
        style=discord.TextStyle.paragraph,
        min_length=10,
        max_length=300,
        custom_id="server_intention",
    )

    def __init__(self, cog: "VerificationCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        db = self.cog.db
        user = interaction.user
        guild = interaction.guild

        if guild is None:
            return

        answers = {
            "fave_waifu": self.fave_waifu.value,
            "age": self.age.value,
            "server_intention": self.server_intention.value,
        }

        # ── Validate age ──────────────────────────────────────────
        try:
            age_val = int(self.age.value)
        except ValueError:
            age_val = -1

        if age_val < 18 or age_val > 99:
            # Log failed attempt
            await db.log_verification_attempt(
                user_id=user.id,
                username=str(user),
                success=False,
                reason="Invalid age or under 18",
                timestamp=datetime.utcnow(),
                guild_id=guild.id,
            )
            embed = discord.Embed(
                title="❌ Verification Failed",
                description="You must be 18 or older to access this server.",
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # ── Age valid — grant role ────────────────────────────────
        settings = await db.get_guild_settings(guild.id)
        role_id = settings.get("verification_role_id") if settings else None
        role = guild.get_role(role_id) if role_id else None

        if role and isinstance(user, discord.Member):
            try:
                await user.add_roles(role, reason="Passed age verification")
            except discord.Forbidden:
                logger.warning(
                    f"Missing permissions to add role {role.id} in guild {guild.id}"
                )

        # Save to DB
        await db.verify_user(
            user_id=user.id,
            username=str(user),
            guild_id=guild.id,
            answers=answers,
        )
        await db.log_verification_attempt(
            user_id=user.id,
            username=str(user),
            success=True,
            reason="Passed",
            timestamp=datetime.utcnow(),
            guild_id=guild.id,
        )

        # Success embed to user
        success_embed = discord.Embed(
            title="✅ Verified!",
            description="Welcome to the server! You now have full access.",
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=success_embed, ephemeral=True)

        # ── Log embed to log channel ──────────────────────────────
        log_channel_id = settings.get("log_channel_id") if settings else None
        if log_channel_id:
            log_channel = guild.get_channel(log_channel_id)
            if log_channel and isinstance(log_channel, discord.TextChannel):
                log_embed = discord.Embed(
                    title="📋 New Verification",
                    color=0x2B2D31,
                    timestamp=datetime.utcnow(),
                )
                log_embed.set_thumbnail(url=user.display_avatar.url)
                log_embed.add_field(
                    name="👤 User",
                    value=f"{user.mention} ({user}) `{user.id}`",
                    inline=False,
                )
                log_embed.add_field(
                    name="🎭 Fave Waifu/Actress",
                    value=answers["fave_waifu"],
                    inline=True,
                )
                log_embed.add_field(
                    name="🎂 Age",
                    value=answers["age"],
                    inline=True,
                )
                log_embed.add_field(
                    name="💬 What they want",
                    value=answers["server_intention"],
                    inline=False,
                )
                log_embed.add_field(
                    name="⏰ Verified At",
                    value=f"<t:{int(datetime.utcnow().timestamp())}:F>",
                    inline=True,
                )
                if role:
                    log_embed.add_field(
                        name="🎭 Role Given",
                        value=role.mention,
                        inline=True,
                    )
                log_embed.set_footer(text=guild.name)

                try:
                    await log_channel.send(embed=log_embed)
                except discord.Forbidden:
                    logger.warning(
                        f"Cannot send log embed in channel {log_channel_id}"
                    )


# ══════════════════════════════════════════════════════════════════
#  Helper — build the verification embed
# ══════════════════════════════════════════════════════════════════

def _build_verification_embed(banner_url: str | None = None) -> discord.Embed:
    """Build the dark-themed verification embed."""
    embed = discord.Embed(
        title="🔞 Age Verification Required",
        description=(
            "**Welcome!**\n\n"
            "This is an **adult-only (18+)** server. To gain access you must "
            "verify your age by clicking the button below.\n\n"
            "• You will be asked a few quick questions.\n"
            "• Your answers are logged for moderation purposes.\n"
            "• **You must be 18 or older to proceed.**\n\n"
            "Click **✅ Verify Me** to begin."
        ),
        color=0x2B2D31,
    )
    if banner_url:
        embed.set_image(url=banner_url)
    embed.set_footer(text="Age verification is required to access this server.")
    return embed


# ══════════════════════════════════════════════════════════════════
#  Cog
# ══════════════════════════════════════════════════════════════════

class VerificationCog(commands.Cog, name="Verification"):
    """Age-verification system with persistent buttons and modals."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self.view = VerificationView(self)

    async def cog_load(self):
        """Register the persistent view and re-attach to existing messages."""
        self.bot.add_view(self.view)
        logger.info("VerificationView registered as persistent view")

        # Re-attach buttons to existing verification messages after restart
        for guild in self.bot.guilds:
            try:
                settings = await self.db.get_guild_settings(guild.id)
                if not settings:
                    continue
                ch_id = settings.get("verification_channel_id")
                msg_id = settings.get("verification_message_id")
                if not ch_id or not msg_id:
                    continue
                channel = guild.get_channel(ch_id)
                if not channel or not isinstance(channel, discord.TextChannel):
                    continue
                try:
                    msg = await channel.fetch_message(msg_id)
                    banner_url = settings.get("verification_banner_url")
                    embed = _build_verification_embed(banner_url)
                    await msg.edit(embed=embed, view=VerificationView(self))
                    logger.info(f"Re-attached verification view in guild {guild.id}")
                except (discord.NotFound, discord.Forbidden):
                    logger.warning(f"Could not re-attach verification view in guild {guild.id}")
            except Exception as e:
                logger.warning(f"Error re-attaching view in guild {guild.id}: {e}")

    # ── /setup_verification ───────────────────────────────────────

    @app_commands.command(
        name="setup_verification",
        description="Set up verification with channel, role, log channel, and optional banner.",
    )
    @app_commands.describe(
        channel="Channel to post the verification embed in",
        role="Role to give upon successful verification",
        log_channel="Channel for verification logs",
        banner="Upload a banner image for the verification embed",
        banner_url="Or paste a direct URL to a banner image instead",
    )
    @app_commands.default_permissions(administrator=True)
    async def setup_verification(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        role: discord.Role,
        log_channel: discord.TextChannel,
        banner: discord.Attachment | None = None,
        banner_url: str | None = None,
    ):
        guild = interaction.guild
        if guild is None:
            return

        await interaction.response.defer(ephemeral=True)

        # ── Resolve banner ────────────────────────────────────────
        file: discord.File | None = None
        embed_image_url: str | None = None

        if banner is not None:
            # Validate it's actually an image
            if not banner.content_type or not banner.content_type.startswith("image/"):
                await interaction.followup.send(
                    "❌ The uploaded file is not an image.", ephemeral=True
                )
                return
            # Download and prepare as File attachment
            image_bytes = await banner.read()
            filename = banner.filename or "banner.png"
            file = discord.File(BytesIO(image_bytes), filename=filename)
            embed_image_url = f"attachment://{filename}"
        elif banner_url:
            embed_image_url = banner_url
        else:
            # Check for previously saved banner
            settings = await self.db.get_guild_settings(guild.id)
            embed_image_url = (
                settings.get("verification_banner_url") if settings else None
            )

        # Build embed + view
        embed = _build_verification_embed(embed_image_url)
        view = VerificationView(self)

        send_kwargs: dict = {"embed": embed, "view": view}
        if file is not None:
            send_kwargs["file"] = file

        try:
            msg = await channel.send(**send_kwargs)
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I don't have permission to send messages in that channel.",
                ephemeral=True,
            )
            return

        # If banner was an attachment, grab the CDN URL from the sent message
        saved_banner_url = banner_url
        if banner is not None and msg.embeds and msg.embeds[0].image:
            saved_banner_url = msg.embeds[0].image.url
        elif banner_url:
            saved_banner_url = banner_url

        # Persist settings
        update_kwargs = dict(
            verification_channel_id=channel.id,
            verification_role_id=role.id,
            log_channel_id=log_channel.id,
            verification_message_id=msg.id,
        )
        if saved_banner_url:
            update_kwargs["verification_banner_url"] = saved_banner_url

        await self.db.update_guild_settings(guild.id, **update_kwargs)

        await interaction.followup.send(
            "✅ Verification channel set up!"
            + (" (with banner)" if embed_image_url else ""),
            ephemeral=True,
        )
        logger.info(
            f"Verification set up in guild {guild.id}: "
            f"channel={channel.id}, role={role.id}, log={log_channel.id}"
        )



    # ── /set_verification_banner ──────────────────────────────────

    @app_commands.command(
        name="set_verification_banner",
        description="Set or update the verification embed banner image.",
    )
    @app_commands.describe(
        banner="Upload a banner image",
        url="Or paste a direct URL to a banner image",
    )
    @app_commands.default_permissions(administrator=True)
    async def set_verification_banner(
        self,
        interaction: discord.Interaction,
        banner: discord.Attachment | None = None,
        url: str | None = None,
    ):
        guild = interaction.guild
        if guild is None:
            return

        settings = await self.db.get_guild_settings(guild.id)
        if not settings or not settings.get("verification_channel_id"):
            await interaction.response.send_message(
                "❌ Run `/setup_verification` first.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        # Resolve banner URL
        banner_url: str | None = None
        file: discord.File | None = None

        if banner is not None:
            if not banner.content_type or not banner.content_type.startswith("image/"):
                await interaction.followup.send(
                    "❌ The uploaded file is not an image.", ephemeral=True
                )
                return
            image_bytes = await banner.read()
            filename = banner.filename or "banner.png"
            file = discord.File(BytesIO(image_bytes), filename=filename)
            banner_url = f"attachment://{filename}"
        elif url:
            banner_url = url
        else:
            await interaction.followup.send(
                "❌ Provide either a file upload or a URL.", ephemeral=True
            )
            return

        # Edit the existing verification embed
        ch_id = settings["verification_channel_id"]
        msg_id = settings.get("verification_message_id")
        channel = guild.get_channel(ch_id)

        saved_url = url  # will be overwritten if attachment
        if channel and msg_id and isinstance(channel, discord.TextChannel):
            try:
                msg = await channel.fetch_message(msg_id)
                embed = _build_verification_embed(banner_url)
                edit_kwargs: dict = {"embed": embed, "view": VerificationView(self)}
                if file is not None:
                    edit_kwargs["attachments"] = [file]
                await msg.edit(**edit_kwargs)

                # Grab CDN URL from the edited message for persistence
                if banner is not None and msg.embeds and msg.embeds[0].image:
                    saved_url = msg.embeds[0].image.url
            except (discord.NotFound, discord.Forbidden):
                logger.warning("Could not edit verification message for banner update")
                await interaction.followup.send(
                    "⚠️ Banner saved but could not update the embed (message missing or no perms).",
                    ephemeral=True,
                )
                return

        if saved_url:
            await self.db.update_guild_settings(guild.id, verification_banner_url=saved_url)
        elif url:
            await self.db.update_guild_settings(guild.id, verification_banner_url=url)

        await interaction.followup.send("✅ Banner updated!", ephemeral=True)

    # ── /verification_status ──────────────────────────────────────

    @app_commands.command(
        name="verification_status",
        description="View verification statistics for this server.",
    )
    @app_commands.default_permissions(administrator=True)
    async def verification_status(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return

        settings = await self.db.get_guild_settings(guild.id)
        stats = await self.db.get_verification_stats(guild.id)

        embed = discord.Embed(
            title="📊 Verification Status",
            color=0x2B2D31,
            timestamp=datetime.utcnow(),
        )
        embed.add_field(
            name="Total Verified",
            value=str(stats["total_verified"]),
            inline=True,
        )
        embed.add_field(
            name="Failed (24 h)",
            value=str(stats["failed_24h"]),
            inline=True,
        )

        if settings:
            v_ch = settings.get("verification_channel_id")
            l_ch = settings.get("log_channel_id")
            r_id = settings.get("verification_role_id")
            embed.add_field(
                name="Verification Channel",
                value=f"<#{v_ch}>" if v_ch else "Not set",
                inline=True,
            )
            embed.add_field(
                name="Log Channel",
                value=f"<#{l_ch}>" if l_ch else "Not set",
                inline=True,
            )
            embed.add_field(
                name="Role",
                value=f"<@&{r_id}>" if r_id else "Not set",
                inline=True,
            )
        else:
            embed.add_field(
                name="Setup",
                value="Not configured — run `/setup_verification`",
                inline=False,
            )

        embed.set_footer(text=guild.name)
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════════
#  Extension setup
# ══════════════════════════════════════════════════════════════════

async def setup(bot: commands.Bot):
    await bot.add_cog(VerificationCog(bot))
    logger.info("VerificationCog loaded")
