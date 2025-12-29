"""Context document builder for Rove.

Aggregates ContextItems into well-structured markdown documents.
"""

import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from openai import AsyncOpenAI

from .config import RoveConfig, load_config
from .database import Database
from .logging import PerformanceTimer, get_logger
from .plugins.base import ContextItem

logger = get_logger("context_builder")


def find_project_root() -> Path:
    """Find the project root directory.

    Looks for git root first, then falls back to current directory.

    Returns:
        The project root path.
    """
    try:
        # Try to find git root
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            git_root = Path(result.stdout.strip())
            if git_root.exists():
                return git_root
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # Fallback to current directory
    return Path.cwd()


class ContextBuilder:
    """Builds context markdown documents from gathered items."""

    def __init__(self, db: Database, config: RoveConfig | None = None):
        """Initialize the context builder.

        Args:
            db: Database instance for metadata storage.
            config: Optional configuration. Loads default if not provided.
        """
        self.db = db
        self.config = config or load_config()
        self._ai_client: AsyncOpenAI | None = None

    def _get_ai_client(self) -> AsyncOpenAI:
        """Get or create the AI client."""
        if self._ai_client is None:
            self._ai_client = AsyncOpenAI(
                base_url=self.config.ai.api_base,
                api_key=self.config.ai.api_key or "dummy",
            )
        return self._ai_client

    async def deduplicate_items(
        self,
        items: list[ContextItem],
        existing_content: str | None = None,
    ) -> list[ContextItem]:
        """Remove semantically duplicate items using AI.

        Args:
            items: List of items to deduplicate.
            existing_content: Optional existing context content to check against.

        Returns:
            Deduplicated list of items.
        """
        if len(items) <= 1:
            return items

        # First pass: URL-based deduplication (fast)
        seen_urls: set[str] = set()
        unique_items: list[ContextItem] = []
        for item in items:
            if item.url not in seen_urls:
                unique_items.append(item)
                seen_urls.add(item.url)

        if len(unique_items) <= 3:
            return unique_items

        # Second pass: AI semantic deduplication
        logger.debug(f"Running AI deduplication on {len(unique_items)} items")

        # Build summaries for comparison
        summaries = []
        for i, item in enumerate(unique_items):
            summary = f"{i}. [{item.source}:{item.item_type}] {item.title}\n{item.content[:300]}"
            summaries.append(summary)

        context_part = ""
        if existing_content:
            context_part = f"\n\nExisting content summary:\n{existing_content[:1000]}..."

        prompt = f"""Identify duplicate or redundant information in these items.
Items with the same information expressed differently should be grouped.
Return the indices of items to KEEP (one from each group of duplicates).
{context_part}

Items:
{chr(10).join(summaries)}

Return ONLY comma-separated indices of items to keep (e.g., "0, 2, 5"):"""

        try:
            client = self._get_ai_client()
            response = await client.chat.completions.create(
                model=self.config.ai.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=100,
                temperature=0.2,
            )
            response_text = response.choices[0].message.content or ""

            # Parse indices
            keep_indices: set[int] = set()
            for part in response_text.replace(",", " ").split():
                try:
                    idx = int(part.strip().rstrip("."))
                    if 0 <= idx < len(unique_items):
                        keep_indices.add(idx)
                except ValueError:
                    continue

            # Always keep at least the first item (primary)
            keep_indices.add(0)

            result = [unique_items[i] for i in sorted(keep_indices)]
            logger.debug(f"Deduplication: {len(unique_items)} -> {len(result)} items")
            return result

        except Exception as e:
            logger.warning(f"AI deduplication failed, using all items: {e}")
            return unique_items

    async def build(
        self,
        ticket_id: str,
        items: list[ContextItem],
        output_dir: Path | None = None,
    ) -> str:
        """Build a context document from items.

        Args:
            ticket_id: The primary ticket ID.
            items: List of context items to include.
            output_dir: Optional output directory. Defaults to .context/

        Returns:
            The filename of the created context file.
        """
        if not items:
            raise ValueError("No items to build context from")

        # Normalize ticket ID to uppercase for consistency
        ticket_id = ticket_id.upper()

        logger.info(f"Building context for {ticket_id} from {len(items)} items")

        with PerformanceTimer(
            "context_build",
            ticket_id=ticket_id,
            items_count=len(items),
        ) as timer:
            # Determine output path first (needed for existing content check)
            if output_dir is None:
                project_root = find_project_root()
                output_dir = project_root / ".context"
                logger.debug(f"Using project root: {project_root}")
            output_dir.mkdir(parents=True, exist_ok=True)

            # Check for existing content for deduplication
            existing_content: str | None = None
            existing_record = await self.db.get_context_file(ticket_id)
            
            # Reuse existing filename if it exists, otherwise generate new one
            if existing_record:
                filename = existing_record.filename
                existing_path = output_dir / filename
                if existing_path.exists():
                    existing_content = existing_path.read_text()
                # Use existing keywords for database update
                keywords = existing_record.keywords
            else:
                # Extract keywords for filename only if no existing file
                logger.debug("Extracting keywords for filename")
                keywords = await self._extract_keywords(items)
                keywords_slug = "_".join(keywords[:4])
                filename = f"{ticket_id}_{keywords_slug}.md"
            
            output_path = output_dir / filename

            # Deduplicate items (AI semantic + URL-based)
            logger.debug("Deduplicating items")
            items = await self.deduplicate_items(items, existing_content)
            timer.add_metric("items_after_dedup", len(items))

            # Group items by topic
            logger.debug("Grouping items by topic via AI")
            grouped = await self._group_by_topic(items)
            timer.add_metric("topic_count", len(grouped))

            # Generate markdown
            logger.debug("Generating markdown document")
            markdown = self._generate_markdown(ticket_id, items, grouped)
            timer.add_metric("markdown_bytes", len(markdown))

            # Write file
            output_path.write_text(markdown)
            logger.debug(f"Wrote context file to {output_path}")

            # Update database
            existing = await self.db.get_context_file(ticket_id)
            if existing:
                await self.db.update_context_file(ticket_id, filename, keywords)
            else:
                await self.db.create_context_file(ticket_id, filename, keywords)

            # Update fetch history for each source
            record = await self.db.get_context_file(ticket_id)
            if record:
                sources_seen = set(item.source for item in items)
                for source in sources_seen:
                    await self.db.update_fetch_history(record.id, source)
                timer.add_metric("sources_updated", len(sources_seen))

        logger.info(f"Context built successfully: {filename}")
        return filename

    async def _extract_keywords(self, items: list[ContextItem]) -> list[str]:
        """Extract keywords from items for filename generation.

        Args:
            items: The context items.

        Returns:
            A list of 3-5 keywords.
        """
        # Combine titles and first part of content
        text_parts = []
        for item in items[:5]:  # Use first 5 items
            text_parts.append(item.title)
            text_parts.append(item.content[:200])

        combined = " ".join(text_parts)

        prompt = f"""Extract 3-5 key technical terms from this text that would make good filename keywords.
Use lowercase, single words only, no special characters.

Text: {combined[:2000]}

Return ONLY comma-separated keywords like: oauth, authentication, api"""

        try:
            client = self._get_ai_client()
            response = await client.chat.completions.create(
                model=self.config.ai.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=50,
                temperature=0.3,
            )
            keywords_text = response.choices[0].message.content or ""
            keywords = [
                re.sub(r"[^a-z0-9]", "", k.strip().lower())
                for k in keywords_text.split(",")
            ]
            return [k for k in keywords if k and len(k) > 2][:5]
        except Exception:
            # Fallback: extract simple keywords
            words = combined.lower().split()
            keywords = [w for w in words if len(w) > 4 and w.isalnum()]
            # Remove common words
            common = {"about", "after", "before", "being", "could", "would", "should"}
            return [w for w in keywords if w not in common][:5]

    async def _group_by_topic(
        self, items: list[ContextItem]
    ) -> dict[str, list[ContextItem]]:
        """Group items by topic using AI.

        Args:
            items: The context items to group.

        Returns:
            A dict mapping topic names to lists of items.
        """
        if len(items) <= 3:
            # Simple grouping by type for small sets
            groups: dict[str, list[ContextItem]] = {}
            for item in items:
                topic = self._type_to_topic(item.item_type)
                if topic not in groups:
                    groups[topic] = []
                groups[topic].append(item)
            return groups

        # Build summaries for AI
        summaries = []
        for i, item in enumerate(items):
            summaries.append(f"{i}. [{item.item_type}] {item.title}")

        prompt = f"""Group these items into 2-4 logical topics for a context document.
Return JSON format like: {{"Topic Name": [0, 2, 3], "Another Topic": [1, 4]}}

Items:
{chr(10).join(summaries)}

Groups (JSON only):"""

        try:
            client = self._get_ai_client()
            response = await client.chat.completions.create(
                model=self.config.ai.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=200,
                temperature=0.3,
            )
            response_text = response.choices[0].message.content or ""

            # Parse JSON from response
            import json

            # Find JSON in response
            json_match = re.search(r"\{[^}]+\}", response_text)
            if json_match:
                grouping = json.loads(json_match.group())
                result: dict[str, list[ContextItem]] = {}
                for topic, indices in grouping.items():
                    result[topic] = [items[i] for i in indices if i < len(items)]
                return result
        except Exception:
            pass

        # Fallback: group by type
        groups = {}
        for item in items:
            topic = self._type_to_topic(item.item_type)
            if topic not in groups:
                groups[topic] = []
            groups[topic].append(item)
        return groups

    def _type_to_topic(self, item_type: str) -> str:
        """Convert item type to topic name."""
        mapping = {
            "ticket": "Primary Ticket",
            "comment": "Discussion",
            "message": "Related Discussions",
            "pr": "Related Code",
            "issue": "Related Issues",
        }
        return mapping.get(item_type, "Other Context")

    def _generate_markdown(
        self,
        ticket_id: str,
        items: list[ContextItem],
        grouped: dict[str, list[ContextItem]],
    ) -> str:
        """Generate the markdown document.

        Args:
            ticket_id: The primary ticket ID.
            items: All context items.
            grouped: Items grouped by topic.

        Returns:
            The markdown content.
        """
        lines: list[str] = []
        references: list[tuple[int, str, str]] = []  # (num, url, title)
        ref_num = 1

        # Find primary ticket title
        primary_title = ticket_id
        for item in items:
            if item.item_type == "ticket" and ticket_id in item.title:
                primary_title = item.title
                break

        # Header
        lines.append(f"# Context: {primary_title}")
        lines.append("")

        # Grouped sections
        for topic, topic_items in grouped.items():
            lines.append(f"## {topic}")
            lines.append("")

            for item in topic_items:
                # Item content
                if item.item_type == "ticket":
                    lines.append(f"### {item.title}")
                else:
                    lines.append(f"### {item.title} [{ref_num}]")
                    references.append((ref_num, item.url, f"{item.source}: {item.title}"))
                    ref_num += 1

                lines.append("")

                # Quote the content
                content_lines = item.content.strip().split("\n")
                for content_line in content_lines[:20]:  # Limit lines
                    lines.append(f"> {content_line}")

                lines.append("")

                # Attribution
                timestamp = item.timestamp.strftime("%b %d, %Y")
                lines.append(f"— *{item.author} via {item.source.upper()}, {timestamp}*")
                lines.append("")

        # Sources table
        lines.append("---")
        lines.append("")
        lines.append("## Sources Consulted")
        lines.append("")
        lines.append("| Source | Items Found | Last Updated |")
        lines.append("|--------|-------------|--------------|")

        source_counts: dict[str, int] = {}
        for item in items:
            source_counts[item.source] = source_counts.get(item.source, 0) + 1

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        for source, count in sorted(source_counts.items()):
            lines.append(f"| {source.upper()} | {count} | {today} |")

        lines.append("")

        # References
        if references:
            lines.append("## References")
            lines.append("")
            for num, url, title in references:
                lines.append(f"[{num}]: {url} \"{title}\"")
            lines.append("")

        return "\n".join(lines)

