import os
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

import motor.motor_asyncio
import pymongo

logger = logging.getLogger(__name__)


class Database:
    """Async MongoDB database handler using motor."""

    def __init__(self):
        self.mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017/r34bot")
        self.client: Optional[motor.motor_asyncio.AsyncIOMotorClient] = None
        self.db = None

        # Collection references (set after initialize)
        self.verified_users = None
        self.guild_settings = None
        self.favorites = None
        self.search_history = None
        self.trending_tags = None
        self.verification_attempts = None

    async def initialize(self):
        """Connect to MongoDB and create indexes."""
        try:
            self.client = motor.motor_asyncio.AsyncIOMotorClient(self.mongo_uri)
            self.db = self.client.get_default_database()

            # Bind collection references
            self.verified_users = self.db["verified_users"]
            self.guild_settings = self.db["guild_settings"]
            self.favorites = self.db["favorites"]
            self.search_history = self.db["search_history"]
            self.trending_tags = self.db["trending_tags"]
            self.verification_attempts = self.db["verification_attempts"]

            # Create indexes
            await self.verified_users.create_index(
                [("user_id", pymongo.ASCENDING), ("guild_id", pymongo.ASCENDING)],
                unique=True,
            )
            await self.guild_settings.create_index(
                [("guild_id", pymongo.ASCENDING)],
                unique=True,
            )
            await self.favorites.create_index(
                [("user_id", pymongo.ASCENDING), ("guild_id", pymongo.ASCENDING)]
            )
            await self.trending_tags.create_index(
                [("count", pymongo.DESCENDING)]
            )
            await self.search_history.create_index(
                [("user_id", pymongo.ASCENDING), ("guild_id", pymongo.ASCENDING)]
            )
            await self.verification_attempts.create_index(
                [("user_id", pymongo.ASCENDING), ("guild_id", pymongo.ASCENDING)]
            )

            # Verify connection
            await self.client.admin.command("ping")
            logger.info("MongoDB connected and indexes created successfully")

        except Exception as e:
            logger.critical(f"Failed to initialize MongoDB: {e}")
            raise

    # ── Verification ──────────────────────────────────────────────

    async def is_user_verified(self, user_id: int, guild_id: int) -> bool:
        """Check if a user is verified in a guild."""
        doc = await self.verified_users.find_one(
            {"user_id": user_id, "guild_id": guild_id}
        )
        return doc is not None

    async def verify_user(
        self,
        user_id: int,
        username: str,
        guild_id: int,
        answers: dict,
        verified_by: str = "self",
    ) -> None:
        """Mark a user as verified, storing their answers."""
        await self.verified_users.update_one(
            {"user_id": user_id, "guild_id": guild_id},
            {
                "$set": {
                    "user_id": user_id,
                    "username": username,
                    "guild_id": guild_id,
                    "verified_at": datetime.utcnow(),
                    "answers": answers,
                    "verified_by": verified_by,
                }
            },
            upsert=True,
        )
        logger.info(f"User {user_id} verified in guild {guild_id}")

    async def log_verification_attempt(
        self,
        user_id: int,
        username: str,
        success: bool,
        reason: str,
        timestamp: datetime,
        guild_id: int,
    ) -> None:
        """Log a verification attempt."""
        await self.verification_attempts.insert_one(
            {
                "user_id": user_id,
                "username": username,
                "success": success,
                "reason": reason,
                "timestamp": timestamp,
                "guild_id": guild_id,
            }
        )

    async def get_verification_stats(self, guild_id: int) -> dict:
        """Return verification statistics for a guild."""
        total_verified = await self.verified_users.count_documents(
            {"guild_id": guild_id}
        )

        twenty_four_hours_ago = datetime.utcnow() - timedelta(hours=24)

        failed_24h = await self.verification_attempts.count_documents(
            {
                "guild_id": guild_id,
                "success": False,
                "timestamp": {"$gte": twenty_four_hours_ago},
            }
        )

        pending_count = await self.verification_attempts.count_documents(
            {
                "guild_id": guild_id,
                "success": False,
                "timestamp": {"$gte": twenty_four_hours_ago},
            }
        )

        return {
            "total_verified": total_verified,
            "pending_count": pending_count,
            "failed_24h": failed_24h,
        }

    async def get_recent_attempt_count(
        self, user_id: int, guild_id: int, hours: int = 1
    ) -> int:
        """Count recent verification attempts (for rate limiting)."""
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        return await self.verification_attempts.count_documents(
            {
                "user_id": user_id,
                "guild_id": guild_id,
                "timestamp": {"$gte": cutoff},
            }
        )

    # ── Guild Settings ────────────────────────────────────────────

    async def get_guild_settings(self, guild_id: int) -> Optional[dict]:
        """Get guild settings document."""
        return await self.guild_settings.find_one({"guild_id": guild_id})

    async def update_guild_settings(self, guild_id: int, **kwargs) -> None:
        """Upsert guild settings."""
        kwargs["updated_at"] = datetime.utcnow()
        await self.guild_settings.update_one(
            {"guild_id": guild_id},
            {"$set": {"guild_id": guild_id, **kwargs}},
            upsert=True,
        )
        logger.info(f"Guild settings updated for {guild_id}")

    # ── Favorites ─────────────────────────────────────────────────

    async def save_favorite(
        self,
        user_id: int,
        guild_id: int,
        post_id: str,
        file_url: str,
        tags: str,
        source: str,
    ) -> bool:
        """Save a post to user favourites. Returns False if duplicate."""
        existing = await self.favorites.find_one(
            {"user_id": user_id, "guild_id": guild_id, "post_id": post_id}
        )
        if existing:
            return False

        await self.favorites.insert_one(
            {
                "user_id": user_id,
                "guild_id": guild_id,
                "post_id": post_id,
                "file_url": file_url,
                "tags": tags,
                "source": source,
                "saved_at": datetime.utcnow(),
            }
        )
        return True

    async def get_favorites(
        self, user_id: int, guild_id: int, limit: int = 10
    ) -> list:
        """Return a user's saved favourites."""
        cursor = (
            self.favorites.find({"user_id": user_id, "guild_id": guild_id})
            .sort("saved_at", pymongo.DESCENDING)
            .limit(limit)
        )
        return await cursor.to_list(length=limit)

    async def remove_favorite(
        self, user_id: int, guild_id: int, post_id: str
    ) -> bool:
        """Remove a favourite. Returns True if something was deleted."""
        result = await self.favorites.delete_one(
            {"user_id": user_id, "guild_id": guild_id, "post_id": post_id}
        )
        return result.deleted_count > 0

    async def get_favorites_count(self, user_id: int, guild_id: int) -> int:
        """Count user favourites in a guild."""
        return await self.favorites.count_documents(
            {"user_id": user_id, "guild_id": guild_id}
        )

    async def clear_favorites(self, user_id: int, guild_id: int) -> int:
        """Clear all favourites for a user in a guild. Returns count deleted."""
        result = await self.favorites.delete_many(
            {"user_id": user_id, "guild_id": guild_id}
        )
        return result.deleted_count

    # ── Search History ────────────────────────────────────────────

    async def log_search(
        self,
        user_id: int,
        guild_id: int,
        query: str,
        source: str,
        result_count: int,
    ) -> None:
        """Log a search query."""
        await self.search_history.insert_one(
            {
                "user_id": user_id,
                "guild_id": guild_id,
                "query": query,
                "source": source,
                "result_count": result_count,
                "searched_at": datetime.utcnow(),
            }
        )

    async def get_user_search_stats(self, user_id: int, guild_id: int) -> dict:
        """Get personal search statistics."""
        total = await self.search_history.count_documents(
            {"user_id": user_id, "guild_id": guild_id}
        )

        # Most searched tag
        pipeline = [
            {"$match": {"user_id": user_id, "guild_id": guild_id}},
            {"$group": {"_id": "$query", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": 1},
        ]
        top_tag_cursor = self.search_history.aggregate(pipeline)
        top_tags = await top_tag_cursor.to_list(length=1)
        most_searched = top_tags[0]["_id"] if top_tags else "None"

        # First search date
        first = await self.search_history.find_one(
            {"user_id": user_id, "guild_id": guild_id},
            sort=[("searched_at", pymongo.ASCENDING)],
        )
        first_date = first["searched_at"] if first else None

        fav_count = await self.get_favorites_count(user_id, guild_id)

        return {
            "total_searches": total,
            "favorite_count": fav_count,
            "most_searched_tag": most_searched,
            "first_search_date": first_date,
        }

    # ── Trending Tags ─────────────────────────────────────────────

    async def increment_tag_trending(self, tag: str) -> None:
        """Increment (or create) a trending tag counter."""
        await self.trending_tags.update_one(
            {"tag": tag},
            {
                "$inc": {"count": 1},
                "$set": {"last_searched": datetime.utcnow()},
            },
            upsert=True,
        )

    async def get_trending_tags(self, limit: int = 10) -> list:
        """Return top trending tags sorted by count descending."""
        cursor = (
            self.trending_tags.find()
            .sort("count", pymongo.DESCENDING)
            .limit(limit)
        )
        return await cursor.to_list(length=limit)

    # ── Lifecycle ─────────────────────────────────────────────────

    async def close(self):
        """Close the MongoDB client connection."""
        if self.client:
            self.client.close()
            logger.info("MongoDB connection closed")
