"""Paradigm Twitter client."""

import asyncio


class PTwitterClient:
    """Sync wrapper around twitter_sdk.TwitterClient."""

    def _run(self, coro):
        return asyncio.run(coro)

    def _get_sdk_client(self):
        from twitter_sdk import TwitterClient

        return TwitterClient()

    def get_user(self, handle: str) -> dict | None:
        """Get user profile by handle."""

        async def _do():
            async with self._get_sdk_client() as client:
                return await client.get_user_by_screen_name(handle)

        return self._run(_do())

    def get_followers(
        self, handle: str, limit: int = 100, ids_only: bool = False
    ) -> tuple[list, dict]:
        """Get followers with pagination."""

        async def _do():
            async with self._get_sdk_client() as client:
                all_followers = []
                cursor = None
                while len(all_followers) < limit:
                    batch_size = min(1000, limit - len(all_followers))
                    followers, cursor, meta = await client.get_followers(
                        handle, cursor=cursor, ids_only=ids_only, max_results=batch_size
                    )
                    all_followers.extend(followers)
                    if not cursor:
                        break
                return all_followers[:limit], meta

        return self._run(_do())

    def get_following(
        self, handle: str, limit: int = 100, ids_only: bool = False
    ) -> tuple[list, dict]:
        """Get following with pagination."""

        async def _do():
            async with self._get_sdk_client() as client:
                all_following = []
                cursor = None
                while len(all_following) < limit:
                    batch_size = min(1000, limit - len(all_following))
                    following, cursor, meta = await client.get_following(
                        handle, cursor=cursor, ids_only=ids_only, max_results=batch_size
                    )
                    all_following.extend(following)
                    if not cursor:
                        break
                return all_following[:limit], meta

        return self._run(_do())

    def lookup_users(self, ids: list[str]) -> list[dict]:
        """Lookup users by IDs."""

        async def _do():
            async with self._get_sdk_client() as client:
                return await client.lookup_users(ids)

        return self._run(_do())

    def search_tweets(
        self, query: str, search_type: str = "latest", limit: int = 20
    ) -> tuple[list, dict]:
        """Search tweets with pagination."""

        async def _do():
            async with self._get_sdk_client() as client:
                all_tweets = []
                cursor = None
                while len(all_tweets) < limit:
                    tweets, cursor, meta = await client.search_tweets(
                        query, search_type=search_type, cursor=cursor
                    )
                    all_tweets.extend(tweets)
                    if not cursor or not tweets:
                        break
                return all_tweets[:limit], meta

        return self._run(_do())

    def lookup_tweets(self, ids: list[str]) -> list[dict]:
        """Lookup tweets by IDs."""

        async def _do():
            async with self._get_sdk_client() as client:
                return await client.lookup_tweets(ids)

        return self._run(_do())

    def get_timeline(self, handle: str, limit: int = 20) -> tuple[dict | None, list, dict | None]:
        """Get user timeline. Returns (user, tweets, meta)."""

        async def _do():
            async with self._get_sdk_client() as client:
                user = await client.get_user_by_screen_name(handle)
                if not user:
                    return None, [], None

                user_id = user.get("user_id")
                all_tweets = []
                cursor = None
                while len(all_tweets) < limit:
                    tweets, cursor, meta = await client.get_user_timeline(user_id, cursor=cursor)
                    all_tweets.extend(tweets)
                    if not cursor or not tweets:
                        break
                return user, all_tweets[:limit], meta

        return self._run(_do())

    def get_usage(self):
        """Check API credit usage."""

        async def _do():
            async with self._get_sdk_client() as client:
                await client.get_user_by_screen_name("twitter")
                return client.get_usage()

        return self._run(_do())


def _client() -> PTwitterClient:
    return PTwitterClient()
