"""AI-assisted search agent for Rove.

The SearchAgent orchestrates multi-phase search across all configured sources,
using AI for keyword extraction, relevance filtering, and reference expansion.
"""

import re
from datetime import datetime

from openai import AsyncOpenAI

from .config import RoveConfig
from .logging import PerformanceTimer, get_logger
from .plugins import get_plugin, list_plugins
from .plugins.base import AuthenticationError, ContextClient, ContextItem

logger = get_logger("search_agent")


class SearchAgent:
    """AI-assisted search agent for context gathering.

    Implements multi-phase search:
    1. Fetch primary ticket from configured source
    2. Extract keywords using AI
    3. Search all sources for references
    4. Expand references (up to max_hops)
    5. Filter results for relevance using AI
    """

    def __init__(self, config: RoveConfig):
        """Initialize the search agent.

        Args:
            config: The Rove configuration.
        """
        self.config = config
        self._ai_client: AsyncOpenAI | None = None
        self._clients: dict[str, ContextClient] = {}

    def _get_ai_client(self) -> AsyncOpenAI:
        """Get or create the AI client."""
        if self._ai_client is None:
            self._ai_client = AsyncOpenAI(
                base_url=self.config.ai.api_base,
                api_key=self.config.ai.api_key or "dummy",  # Some providers don't need keys
            )
        return self._ai_client

    def _get_source_client(self, source: str) -> ContextClient | None:
        """Get or create a client for a source."""
        if source not in self._clients:
            factory = get_plugin(source)
            if factory:
                # Get source-specific config including OAuth credentials
                source_config: dict = {}
                if hasattr(self.config.sources, source):
                    src_cfg = getattr(self.config.sources, source)
                    source_config = {
                        "rate_limit": src_cfg.rate_limit,
                        "page_size": src_cfg.page_size,
                    }
                    # Include OAuth credentials if configured
                    if src_cfg.client_id:
                        source_config["client_id"] = src_cfg.client_id
                    if src_cfg.client_secret:
                        source_config["client_secret"] = src_cfg.client_secret
                    # Include GitHub-specific config
                    if hasattr(src_cfg, "default_owner") and src_cfg.default_owner:
                        source_config["default_owner"] = src_cfg.default_owner
                    if hasattr(src_cfg, "default_repo") and src_cfg.default_repo:
                        source_config["default_repo"] = src_cfg.default_repo
                self._clients[source] = factory(source_config)
        return self._clients.get(source)

    async def search(
        self,
        ticket_id: str,
        source_override: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[ContextItem]:
        """Search for context related to a ticket.

        Args:
            ticket_id: The ticket ID to search for.
            source_override: Override the default primary source.
            since: Only include items after this date.
            until: Only include items before this date.

        Returns:
            A list of relevant ContextItem objects.
        """
        primary_source = source_override or self.config.sources.default_ticket_source
        all_items: list[ContextItem] = []
        seen_urls: set[str] = set()

        # Normalize ticket ID to uppercase for consistency
        ticket_id = ticket_id.upper()

        logger.info(f"Starting search for {ticket_id} from {primary_source}")

        # Phase 1: Fetch primary ticket
        logger.debug(f"Phase 1: Fetching primary ticket from {primary_source}")
        primary_client = self._get_source_client(primary_source)
        if not primary_client:
            logger.warning(f"No client available for source: {primary_source}")
            return []

        # Ensure authenticated
        if not primary_client.is_authenticated():
            logger.debug(f"Authenticating with {primary_source}")
            if not await primary_client.authenticate():
                logger.error(f"Authentication failed for {primary_source}")
                raise AuthenticationError(
                    f"Failed to authenticate with {primary_source}. "
                    f"Run 'rove --add-source {primary_source}' to re-authenticate."
                )

        primary_item = await primary_client.get_item_details(ticket_id)
        if not primary_item:
            logger.warning(f"Primary ticket {ticket_id} not found in {primary_source}")
            return []

        all_items.append(primary_item)
        seen_urls.add(primary_item.url)

        # Phase 2: Extract keywords from primary ticket
        logger.debug("Phase 2: Extracting keywords via AI")
        keywords = await self._extract_keywords(primary_item)
        logger.debug(f"Extracted keywords: {keywords}")

        # Phase 3: Search all sources for ticket ID and keywords
        logger.debug("Phase 3: Searching all sources")
        search_queries = [ticket_id] + keywords[:5]  # Limit keywords

        for source_name in list_plugins():
            client = self._get_source_client(source_name)
            if not client or not client.is_authenticated():
                continue

            for query in search_queries:
                try:
                    items = await client.search(
                        query=query,
                        since=since,
                        until=until,
                    )
                    for item in items:
                        if item.url not in seen_urls:
                            all_items.append(item)
                            seen_urls.add(item.url)
                except Exception as e:
                    logger.debug(f"Search failed for {query} in {source_name}: {e}")
                    continue  # Skip failed searches

        logger.debug(f"Found {len(all_items)} items after source search")

        # Phase 4: Multi-hop expansion (find referenced items)
        logger.debug("Phase 4: Multi-hop reference expansion")
        references = self._extract_references(all_items)
        hop_count = 1
        max_hops = self.config.ai.max_hops

        while references and hop_count < max_hops:
            new_references: list[tuple[str, str]] = []

            for ref_type, ref_id in references:
                item = await self._expand_reference(ref_type, ref_id)
                if item and item.url not in seen_urls:
                    all_items.append(item)
                    seen_urls.add(item.url)
                    # Extract references from new item
                    new_refs = self._extract_references([item])
                    new_references.extend(new_refs)

            references = new_references
            hop_count += 1
            logger.debug(f"Hop {hop_count}: found {len(new_references)} new references")

        # Phase 5: Filter for relevance using AI
        logger.debug(f"Phase 5: Filtering {len(all_items)} items for relevance")
        if len(all_items) > 1:
            all_items = await self._filter_relevant(all_items, primary_item)

        logger.info(f"Search complete for {ticket_id}: {len(all_items)} relevant items found")
        return all_items

    async def _extract_keywords(self, item: ContextItem) -> list[str]:
        """Use AI to extract search keywords from content.

        Args:
            item: The primary item to extract keywords from.

        Returns:
            A list of keyword strings.
        """
        prompt = f"""Extract 3-5 key technical terms or concepts from this ticket that would help find related discussions.

Title: {item.title}
Content: {item.content[:2000]}

Return ONLY a comma-separated list of keywords, nothing else.
Example: authentication, OAuth2, API keys, enterprise SSO"""

        try:
            client = self._get_ai_client()
            response = await client.chat.completions.create(
                model=self.config.ai.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=100,
                temperature=0.3,
            )
            keywords_text = response.choices[0].message.content or ""
            # Parse comma-separated keywords
            keywords = [k.strip() for k in keywords_text.split(",") if k.strip()]
            return keywords
        except Exception:
            # Fallback: extract simple keywords from title
            words = item.title.split()
            return [w for w in words if len(w) > 3 and w.isalnum()][:5]

    def _extract_references(self, items: list[ContextItem]) -> list[tuple[str, str]]:
        """Extract references to other items from content.

        Args:
            items: List of items to scan for references.

        Returns:
            List of (reference_type, reference_id) tuples.
        """
        references: list[tuple[str, str]] = []
        seen: set[str] = set()

        patterns = [
            # JIRA tickets: ABC-123
            (r"\b([A-Z]{2,10}-\d+)\b", "ticket"),
            # GitHub PRs: PR #123, pull #123
            (r"\b(?:PR|pull)\s*#?(\d+)\b", "pr"),
            # GitHub issues: issue #123, #123 (in context)
            (r"\bissue\s*#?(\d+)\b", "issue"),
        ]

        for item in items:
            text = f"{item.title} {item.content}"
            for pattern, ref_type in patterns:
                for match in re.finditer(pattern, text, re.IGNORECASE):
                    ref_id = match.group(1)
                    key = f"{ref_type}:{ref_id}"
                    if key not in seen:
                        references.append((ref_type, ref_id))
                        seen.add(key)

        return references

    async def _expand_reference(
        self, ref_type: str, ref_id: str
    ) -> ContextItem | None:
        """Fetch details for a referenced item.

        Dynamically finds plugins that support the given reference type
        and attempts to resolve the reference through each one.

        Args:
            ref_type: The type of reference (ticket, pr, issue, etc.).
            ref_id: The reference identifier.

        Returns:
            A ContextItem if found, None otherwise.
        """
        # Find all plugins that support this reference type
        for source_name in list_plugins():
            client = self._get_source_client(source_name)
            if not client:
                continue

            # Check if this plugin supports the reference type
            if ref_type not in client.supported_reference_types():
                continue

            # Only try authenticated clients
            if not client.is_authenticated():
                continue

            try:
                item = await client.get_item_details(ref_id)
                if item:
                    return item
            except Exception:
                continue  # Try next plugin

        return None

    async def _filter_relevant(
        self, items: list[ContextItem], primary: ContextItem
    ) -> list[ContextItem]:
        """Use AI to filter items for relevance.

        Args:
            items: All gathered items.
            primary: The primary ticket for context.

        Returns:
            Filtered list of relevant items.
        """
        if len(items) <= 10:
            return items  # Don't filter small sets

        # Pre-filter: prioritize items that mention the ticket ID in title
        ticket_id = primary.title.split(":")[0].strip() if ":" in primary.title else ""
        
        # Separate items into tiers
        tier1_items = []  # Explicitly mention ticket ID
        tier2_items = []  # From same sources as primary, or PRs/issues
        tier3_items = []  # Everything else
        
        for item in items:
            if ticket_id and ticket_id.upper() in item.title.upper():
                tier1_items.append(item)
            elif item.item_type in ("pr", "issue", "ticket"):
                tier2_items.append(item)
            else:
                tier3_items.append(item)
        
        # Limit items to send to AI (prioritize tier1 and tier2)
        max_items_for_ai = 50
        items_for_ai = tier1_items + tier2_items
        if len(items_for_ai) < max_items_for_ai:
            items_for_ai.extend(tier3_items[:max_items_for_ai - len(items_for_ai)])
        else:
            items_for_ai = items_for_ai[:max_items_for_ai]
        
        logger.debug(
            f"Pre-filtered to {len(items_for_ai)} items for AI "
            f"(tier1={len(tier1_items)}, tier2={len(tier2_items)}, tier3={len(tier3_items)})"
        )

        # Build item summaries for AI
        summaries = []
        for i, item in enumerate(items_for_ai):
            summaries.append(
                f"{i}. [{item.source}] {item.title}: {item.content[:150]}..."
            )

        prompt = f"""Given this primary ticket:
Title: {primary.title}
Description: {primary.content[:500]}

Which of these items are relevant to understanding or implementing this ticket?
Be inclusive - include items that discuss the same feature, related PRs, design discussions, etc.
Return ONLY the numbers of relevant items, comma-separated.

Items:
{chr(10).join(summaries)}

Relevant item numbers:"""

        try:
            client = self._get_ai_client()
            response = await client.chat.completions.create(
                model=self.config.ai.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,  # Enough room for many item numbers
                temperature=0.3,
            )
            response_text = response.choices[0].message.content or ""
            logger.debug(f"AI relevance filter response: {response_text[:200]}")

            # Parse numbers from response
            relevant_indices: set[int] = set()
            for part in response_text.replace(",", " ").split():
                try:
                    idx = int(part.strip().rstrip("."))
                    if 0 <= idx < len(items_for_ai):
                        relevant_indices.add(idx)
                except ValueError:
                    continue

            # Always include primary (index 0 if it's there)
            relevant_indices.add(0)

            filtered = [items_for_ai[i] for i in sorted(relevant_indices)]
            logger.debug(f"AI selected {len(filtered)} relevant items")
            
            # If AI returned very few items, include tier1 items anyway
            if len(filtered) < 5 and tier1_items:
                for item in tier1_items:
                    if item not in filtered:
                        filtered.append(item)
                logger.debug(f"Added tier1 items, now have {len(filtered)} items")
            
            return filtered
        except Exception as e:
            logger.warning(f"AI relevance filter failed: {e}, returning pre-filtered items")
            # Return tier1 + tier2 items as fallback
            return (tier1_items + tier2_items)[:50] or items[:20]

