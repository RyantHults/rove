"""Ticket analyzer for identifying gaps and suggesting improvements.

Analyzes context files to help improve ticket quality by identifying
missing details, ambiguities, and questions that need answers.
"""

import re
from dataclasses import dataclass

from openai import AsyncOpenAI

from .config import RoveConfig, load_config
from .logging import get_logger

logger = get_logger("ticket_analyzer")


@dataclass
class EpicAnalysisResult:
    """Result from Phase 1 epic-level analysis."""

    summary: str
    epic_gaps: list[str]
    tickets_needing_work: list[str]  # Ticket IDs that need deeper analysis


@dataclass
class TicketSection:
    """A parsed ticket section from the context file."""

    ticket_id: str
    title: str
    content: str  # Full content including comments


class TicketAnalyzer:
    """Analyzes context files to identify gaps and suggest improvements.

    Uses a two-phase approach:
    1. Epic-level analysis to get summary, gaps, and flag problematic tickets
    2. Targeted deep-dive on only the flagged tickets
    """

    def __init__(self, config: RoveConfig | None = None):
        """Initialize the analyzer.

        Args:
            config: Optional configuration. Loads default if not provided.
        """
        self.config = config or load_config()
        self._ai_client: AsyncOpenAI | None = None

    def _get_ai_client(self) -> AsyncOpenAI:
        """Get or create the AI client."""
        if self._ai_client is None:
            self._ai_client = AsyncOpenAI(
                base_url=self.config.ai.api_base,
                api_key=self.config.ai.api_key or "dummy",
                timeout=60.0,  # Longer timeout for analysis
            )
        return self._ai_client

    def _parse_tickets(self, context_content: str) -> list[TicketSection]:
        """Parse the context file to extract individual ticket sections.

        Args:
            context_content: The full context file content.

        Returns:
            List of TicketSection objects.
        """
        tickets: list[TicketSection] = []

        # Match ticket headers like "### TB-291: Title [ref]" or "### TB-291: Title"
        # Also match comment headers like "### Comment on TB-292 [ref]"
        ticket_pattern = r"^### ((?:[A-Z]+-\d+)[^[\n]*?)(?:\s*\[\d+\])?\s*$"

        lines = context_content.split("\n")
        current_ticket: TicketSection | None = None
        current_content_lines: list[str] = []

        for line in lines:
            match = re.match(ticket_pattern, line)
            if match:
                # Save previous ticket if exists
                if current_ticket:
                    current_ticket.content = "\n".join(current_content_lines).strip()
                    tickets.append(current_ticket)

                # Parse new ticket
                full_title = match.group(1).strip()
                # Extract ticket ID from title (e.g., "TB-291: Some title" -> "TB-291")
                ticket_id_match = re.match(r"([A-Z]+-\d+)", full_title)
                if ticket_id_match:
                    ticket_id = ticket_id_match.group(1)
                else:
                    # For comments, extract from "Comment on TB-292"
                    comment_match = re.search(r"([A-Z]+-\d+)", full_title)
                    ticket_id = comment_match.group(1) if comment_match else "UNKNOWN"

                current_ticket = TicketSection(
                    ticket_id=ticket_id,
                    title=full_title,
                    content="",
                )
                current_content_lines = []
            elif current_ticket:
                # Stop at section boundaries
                if line.startswith("## ") or line.startswith("---"):
                    current_ticket.content = "\n".join(current_content_lines).strip()
                    tickets.append(current_ticket)
                    current_ticket = None
                    current_content_lines = []
                else:
                    current_content_lines.append(line)

        # Don't forget the last ticket
        if current_ticket:
            current_ticket.content = "\n".join(current_content_lines).strip()
            tickets.append(current_ticket)

        return tickets

    def _extract_ticket_content(
        self, context_content: str, ticket_id: str
    ) -> str:
        """Extract all content related to a specific ticket ID.

        Args:
            context_content: The full context file content.
            ticket_id: The ticket ID to extract (e.g., "TB-291").

        Returns:
            Combined content for the ticket including comments.
        """
        tickets = self._parse_tickets(context_content)
        relevant = [t for t in tickets if t.ticket_id == ticket_id]

        if not relevant:
            return ""

        parts = []
        for ticket in relevant:
            parts.append(f"### {ticket.title}\n\n{ticket.content}")

        return "\n\n".join(parts)

    async def _analyze_epic(self, context_content: str) -> EpicAnalysisResult:
        """Phase 1: Analyze the epic at a high level.

        Args:
            context_content: The full context file content.

        Returns:
            EpicAnalysisResult with summary, gaps, and flagged tickets.
        """
        prompt = f"""Analyze this JIRA epic/ticket context and provide:

1. A 2-3 paragraph SUMMARY of what this epic is trying to accomplish (business goal, key components, overall approach).

2. EPIC-LEVEL GAPS - High-level design issues that affect multiple tickets or architectural questions that might not have been considered:
   - Conflicting technical approaches between tickets
   - Duplicate tickets covering the same implementation
   - Shared components, services, or data models that aren't consistently defined
   - Cross-cutting dependencies or integration points that are unclear
   - Scope ambiguities that span multiple tickets

   DO NOT include:
   - Missing features that might be in other tickets (billing, analytics, etc.)
   - Product-level concerns (success metrics, adoption strategy)
   - Things that are simply "not mentioned" but aren't needed for implementation

3. TICKETS NEEDING WORK - List ticket IDs (e.g., ticket-123, ticket-456) where a developer would be BLOCKED due to missing scope, dependency, or design clarity. Flag tickets where:
   - Systems, services, or components impacted are unclear
   - Upstream or downstream dependencies aren't specified
   - Dependencies on other tickets, teams, or vendors are ambiguous
   - Scope (what's in/out) is not clearly defined
   - It's unclear if this modifies existing behavior or creates new behavior
   - Assumptions are baked into the ticket without being explicit

   DO NOT flag tickets just because they lack product details - focus on what's needed to understand SCOPE and DEPENDENCIES.

Format your response EXACTLY like this:

## Summary
[Your 2-3 paragraph summary here]

## Epic-Level Gaps
- [Gap 1]
- [Gap 2]
...

## Tickets Needing Work
TB-291, TB-292, TB-294

---

Context file:
{context_content}
"""

        try:
            client = self._get_ai_client()
            response = await client.chat.completions.create(
                model=self.config.ai.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2000,
                temperature=0.3,
            )
            response_text = response.choices[0].message.content or ""
            logger.debug(f"Epic analysis response length: {len(response_text)}")

            # Parse the response
            return self._parse_epic_response(response_text)

        except Exception as e:
            logger.error(f"Epic analysis failed: {e}")
            # Return a minimal result on failure
            return EpicAnalysisResult(
                summary="Analysis failed - please try again.",
                epic_gaps=[f"Error during analysis: {e}"],
                tickets_needing_work=[],
            )

    def _parse_epic_response(self, response_text: str) -> EpicAnalysisResult:
        """Parse the AI response into structured data.

        Args:
            response_text: The raw AI response.

        Returns:
            Parsed EpicAnalysisResult.
        """
        summary = ""
        epic_gaps: list[str] = []
        tickets_needing_work: list[str] = []

        # Extract summary section
        summary_match = re.search(
            r"## Summary\s*\n(.*?)(?=\n## |\Z)",
            response_text,
            re.DOTALL,
        )
        if summary_match:
            summary = summary_match.group(1).strip()

        # Extract epic-level gaps
        gaps_match = re.search(
            r"## Epic-Level Gaps\s*\n(.*?)(?=\n## |\Z)",
            response_text,
            re.DOTALL,
        )
        if gaps_match:
            gaps_text = gaps_match.group(1).strip()
            # Parse bullet points
            for line in gaps_text.split("\n"):
                line = line.strip()
                if line.startswith("- "):
                    epic_gaps.append(line[2:].strip())
                elif line.startswith("* "):
                    epic_gaps.append(line[2:].strip())

        # Extract tickets needing work
        tickets_match = re.search(
            r"## Tickets Needing Work\s*\n(.*?)(?=\n---|\Z)",
            response_text,
            re.DOTALL,
        )
        if tickets_match:
            tickets_text = tickets_match.group(1).strip()
            # Find all ticket IDs
            ticket_ids = re.findall(r"[A-Z]+-\d+", tickets_text)
            tickets_needing_work = list(dict.fromkeys(ticket_ids))  # Dedupe, preserve order

        return EpicAnalysisResult(
            summary=summary,
            epic_gaps=epic_gaps,
            tickets_needing_work=tickets_needing_work,
        )

    async def _analyze_ticket(
        self, ticket_content: str, ticket_id: str, epic_summary: str
    ) -> str:
        """Phase 2: Deep-dive analysis of a specific ticket.

        Args:
            ticket_content: The ticket's content including comments.
            ticket_id: The ticket ID being analyzed.
            epic_summary: The epic summary for context.

        Returns:
            Markdown section with gap analysis for this ticket.
        """
        prompt = f"""You are analyzing a specific ticket that was flagged as needing technical clarification.

EPIC CONTEXT (what this ticket is part of):
{epic_summary}

TICKET TO ANALYZE ({ticket_id}):
{ticket_content}

---

Identify high-level DESIGN questions that must be answered before a developer can implement this. Focus on questions like:

- What systems, services, or components are impacted?
- Are there upstream or downstream dependencies?
- Does this depend on another ticket, team, or vendor?
- What is explicitly in scope?
- What is explicitly out of scope?
- Are we modifying existing behavior or creating new behavior?
- Are there assumptions baked into the ticket?

Group questions by topic areas such as:
- **Scope & Boundaries**: What's in/out of scope, what's being modified vs. created
- **Dependencies**: Other tickets, teams, services, vendors
- **Systems Impact**: Which components/services are affected
- **Data & Schema**: What data structures are needed (if mentioned in ticket)
- **API & Contracts**: What endpoints/contracts are needed (if mentioned in ticket)
- **Business Logic**: What are the rules and edge cases (if mentioned in ticket)
- **Integration Points**: How this interacts with existing systems (if mentioned in ticket)

DO NOT ask about:
- Features not mentioned in this ticket (they may be in other tickets)
- Product strategy, metrics, or analytics
- General best practices that the developer can decide
- Low-level implementation details that can be inferred from the requirements

Group your questions by topic. Format your response like this:

**[Topic 1]:**
- [Question 1]
- [Question 2]

**[Topic 2]:**
- [Question 1]
...

IMPORTANT:
- Stay focused on what IS mentioned in the ticket - don't expand scope
- Be specific and implementation-focused
- Infer technical questions from the requirements (e.g., if they mention "limits", ask about enforcement behavior)
- Do NOT include any summary, conclusion, or wrap-up text at the end
- End your response with the last question, nothing more
"""

        try:
            client = self._get_ai_client()
            response = await client.chat.completions.create(
                model=self.config.ai.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1000,
                temperature=0.3,
            )
            response_text = response.choices[0].message.content or ""
            logger.debug(f"Ticket {ticket_id} analysis response length: {len(response_text)}")
            return response_text.strip()

        except Exception as e:
            logger.error(f"Ticket analysis failed for {ticket_id}: {e}")
            return f"*Analysis failed: {e}*"

    def _get_ticket_title(self, context_content: str, ticket_id: str) -> str:
        """Get the title for a ticket from the context file.

        Args:
            context_content: The full context file content.
            ticket_id: The ticket ID to find.

        Returns:
            The ticket title, or just the ID if not found.
        """
        # Look for "### TB-123: Some Title"
        pattern = rf"### ({ticket_id}:[^\[\n]+)"
        match = re.search(pattern, context_content)
        if match:
            return match.group(1).strip()
        return ticket_id

    async def analyze(self, ticket_id: str, context_content: str) -> str:
        """Analyze a context file and generate suggestions.

        Args:
            ticket_id: The primary ticket ID (for output naming).
            context_content: The full context file content.

        Returns:
            The complete suggestions markdown document.
        """
        logger.info(f"Starting analysis for {ticket_id}")

        # Phase 1: Epic-level analysis
        logger.info("Phase 1: Analyzing epic...")
        epic_result = await self._analyze_epic(context_content)

        logger.info(
            f"Phase 1 complete: {len(epic_result.epic_gaps)} epic gaps, "
            f"{len(epic_result.tickets_needing_work)} tickets flagged"
        )

        # Phase 2: Deep-dive on flagged tickets
        ticket_sections: list[str] = []
        if epic_result.tickets_needing_work:
            logger.info(
                f"Phase 2: Analyzing {len(epic_result.tickets_needing_work)} flagged tickets..."
            )
            for flagged_id in epic_result.tickets_needing_work:
                ticket_content = self._extract_ticket_content(context_content, flagged_id)
                if not ticket_content:
                    logger.warning(f"Could not extract content for {flagged_id}")
                    continue

                ticket_title = self._get_ticket_title(context_content, flagged_id)
                section_content = await self._analyze_ticket(
                    ticket_content, flagged_id, epic_result.summary
                )
                ticket_sections.append(f"### {ticket_title}\n\n{section_content}")

        # Assemble the final document
        return self._build_output(ticket_id, epic_result, ticket_sections)

    def _build_output(
        self,
        ticket_id: str,
        epic_result: EpicAnalysisResult,
        ticket_sections: list[str],
    ) -> str:
        """Build the final suggestions markdown document.

        Args:
            ticket_id: The primary ticket ID.
            epic_result: The epic-level analysis result.
            ticket_sections: List of per-ticket analysis sections.

        Returns:
            The complete markdown document.
        """
        lines: list[str] = []

        # Header
        lines.append(f"# Suggestions: {ticket_id}")
        lines.append("")

        # Summary
        lines.append("## Summary")
        lines.append("")
        lines.append(epic_result.summary)
        lines.append("")

        # Epic-level gaps
        if epic_result.epic_gaps:
            lines.append("## Epic-Level Gaps")
            lines.append("")
            for gap in epic_result.epic_gaps:
                lines.append(f"- {gap}")
            lines.append("")

        # Tickets needing work
        if ticket_sections:
            lines.append("## Tickets Needing Work")
            lines.append("")
            for section in ticket_sections:
                lines.append(section)
                lines.append("")
        elif not epic_result.tickets_needing_work:
            lines.append("## Tickets Needing Work")
            lines.append("")
            lines.append("*No tickets were flagged as needing additional work.*")
            lines.append("")

        return "\n".join(lines)
