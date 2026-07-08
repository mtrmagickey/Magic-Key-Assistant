"""
Feedback View - Attach to every LLM response for learning loop
"""

import json
import logging
from datetime import datetime
from typing import List, Optional

import discord

logger = logging.getLogger(__name__)


class ResponseFeedbackView(discord.ui.View):
    """Buttons for feedback on bot responses - drives learning loop"""
    
    def __init__(self, question: str, answer: str, db_pool, question_id: int = None, chunk_sources: List[str] = None):
        super().__init__(timeout=None)  # Persistent buttons
        self.question = question
        self.answer = answer
        self.db_pool = db_pool
        self.question_id = question_id  # For Steward tracking
        self.chunk_sources = chunk_sources or []
        self.feedback_given = False
    
    @discord.ui.button(label="👍 Helpful", style=discord.ButtonStyle.success, custom_id="feedback_helpful")
    async def helpful_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """User found response helpful"""
        if self.feedback_given:
            await interaction.response.send_message("You've already given feedback, thanks!", ephemeral=True)
            return
        
        self.feedback_given = True
        await self._record_feedback(interaction, "helpful")
        
        # Update button to show selection
        button.style = discord.ButtonStyle.primary
        button.label = "✅ Marked Helpful"
        
        # Disable both buttons
        for item in self.children:
            item.disabled = True
        
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("Thanks for the feedback! 💙", ephemeral=True)
    
    @discord.ui.button(label="👎 Not Helpful", style=discord.ButtonStyle.secondary, custom_id="feedback_not_helpful")
    async def not_helpful_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """User found response unhelpful - trigger improvement flow"""
        if self.feedback_given:
            await interaction.response.send_message("You've already given feedback, thanks!", ephemeral=True)
            return
        
        self.feedback_given = True
        await self._record_feedback(interaction, "not_helpful")
        
        # Update button to show selection
        button.style = discord.ButtonStyle.danger
        button.label = "❌ Marked Not Helpful"
        
        # Disable both buttons
        for item in self.children:
            item.disabled = True
        
        await interaction.response.edit_message(view=self)
        
        # Offer improvement modal
        await interaction.followup.send(
            "Sorry that didn't help! I've logged this to improve. Want to tell me more?",
            view=ImprovementFollowUpView(self.question, self.answer),
            ephemeral=True
        )
    
    async def _record_feedback(self, interaction: discord.Interaction, feedback: str):
        """Record feedback to database"""
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO response_feedback 
                    (user_id, username, question, answer, feedback, channel_id, message_id, chunk_sources)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                interaction.user.id,
                str(interaction.user),
                self.question,
                self.answer[:2000],  # Truncate if needed
                feedback,
                interaction.channel_id,
                interaction.message.id if interaction.message else None,
                json.dumps(self.chunk_sources) if self.chunk_sources else None,
                ))
                # aiosqlite does not autocommit
                await conn.commit()
            
            logger.info(f"Recorded {feedback} feedback from {interaction.user} on question: {self.question[:50]}")
            
            # Update Steward tracking if question_id exists
            if self.question_id:
                bot = interaction.client
                auto_ops = bot.get_cog("AutonomousOps")
                if auto_ops and hasattr(auto_ops, '_steward_update_question_feedback'):
                    quality = "helpful" if feedback == "helpful" else "unhelpful"
                    await auto_ops._steward_update_question_feedback(self.question_id, quality)
            
            # If negative, check if we should auto-create improvement memo
            if feedback == "not_helpful":
                await self._check_auto_improvement_memo(interaction)
                
        except Exception as e:
            logger.error(f"Failed to record feedback: {e}")
    
    async def _check_auto_improvement_memo(self, interaction: discord.Interaction):
        """Check if this negative feedback warrants an auto-improvement memo"""
        try:
            # Count recent negative feedback on similar questions
            async with self.db_pool.acquire() as conn:
                # SQLite syntax: LIKE, ?, datetime
                async with conn.execute("""
                    SELECT COUNT(*) FROM response_feedback
                    WHERE feedback = 'not_helpful'
                    AND question LIKE ?
                    AND created_at > datetime('now', '-7 days')
                """, (f"%{self.question[:100]}%",)) as cursor:
                    row = await cursor.fetchone()
                    count = row[0] if row else 0
                
                # If 2+ negative feedbacks on similar topic in a week, flag for improvement
                if count >= 2:
                    # Find DocumentAuthor cog and create improvement memo
                    bot = interaction.client
                    doc_author = bot.get_cog("DocumentAuthor")
                    
                    if doc_author:
                        memo_topic = f"Improvement: {self.question[:80]}"
                        await doc_author.auto_create_improvement_memo(
                            topic=memo_topic,
                            question=self.question,
                            answer=self.answer,
                            feedback_count=count
                        )
                        logger.info(f"Auto-created improvement memo for repeated negative feedback: {memo_topic}")
        
        except Exception as e:
            logger.error(f"Failed to check auto-improvement: {e}")


class PersistentFeedbackView(discord.ui.View):
    """Persistent view for handling feedback buttons after bot restart.
    
    This view is registered on startup and handles interactions for old messages
    where the original ResponseFeedbackView context is no longer available.
    """
    
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot
    
    @discord.ui.button(label="👍 Helpful", style=discord.ButtonStyle.success, custom_id="feedback_helpful")
    async def helpful_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle helpful feedback from old messages"""
        try:
            await self._record_feedback(interaction, "helpful")
            
            # Disable buttons on the original message
            view = discord.ui.View(timeout=None)
            done_button = discord.ui.Button(label="✅ Marked Helpful", style=discord.ButtonStyle.primary, disabled=True)
            view.add_item(done_button)
            
            await interaction.response.edit_message(view=view)
            await interaction.followup.send("Thanks for the feedback! 💙", ephemeral=True)
        except Exception as e:
            logger.error(f"Persistent feedback helpful failed: {e}")
            await interaction.response.send_message("Thanks for the feedback! 💙", ephemeral=True)
    
    @discord.ui.button(label="👎 Not Helpful", style=discord.ButtonStyle.secondary, custom_id="feedback_not_helpful")
    async def not_helpful_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle not helpful feedback from old messages"""
        try:
            await self._record_feedback(interaction, "not_helpful")
            
            # Disable buttons on the original message
            view = discord.ui.View(timeout=None)
            done_button = discord.ui.Button(label="❌ Marked Not Helpful", style=discord.ButtonStyle.danger, disabled=True)
            view.add_item(done_button)
            
            await interaction.response.edit_message(view=view)
            await interaction.followup.send(
                "Sorry that didn't help! I've logged this to improve.",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Persistent feedback not_helpful failed: {e}")
            await interaction.response.send_message(
                "Sorry that didn't help! I've logged this to improve.",
                ephemeral=True
            )
    
    async def _record_feedback(self, interaction: discord.Interaction, feedback: str):
        """Record feedback to database"""
        try:
            db = getattr(self.bot, 'db', None)
            if not db:
                return
            
            # Try to extract question from the message content
            question = "Unknown (from persistent view)"
            answer = "Unknown (from persistent view)"
            if interaction.message:
                # The answer is typically the message content
                answer = interaction.message.content[:2000] if interaction.message.content else answer
            
            await db.execute("""
                INSERT INTO response_feedback
                (user_id, username, question, answer, feedback, channel_id, message_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                interaction.user.id,
                str(interaction.user),
                question,
                answer,
                feedback,
                interaction.channel_id,
                interaction.message.id if interaction.message else None
                ))
            logger.info(f"Recorded {feedback} feedback (persistent) from {interaction.user}")
                
        except Exception as e:
            logger.error(f"Failed to record persistent feedback: {e}")


class ImprovementFollowUpView(discord.ui.View):
    """Optional follow-up to gather more context on negative feedback"""
    
    def __init__(self, question: str, answer: str):
        super().__init__(timeout=300)  # 5 minutes
        self.question = question
        self.answer = answer
    
    @discord.ui.button(label="💬 Add Context", style=discord.ButtonStyle.primary)
    async def add_context(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show modal to gather improvement details"""
        modal = ImprovementModal(self.question, self.answer)
        await interaction.response.send_modal(modal)
        self.stop()
    
    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Skip additional feedback"""
        await interaction.response.send_message("No problem, thanks for the feedback!", ephemeral=True)
        self.stop()


class ImprovementModal(discord.ui.Modal, title="Help Me Improve"):
    """Modal to gather detailed feedback on unhelpful responses"""
    
    what_was_wrong = discord.ui.TextInput(
        label="What was wrong or missing?",
        style=discord.TextStyle.paragraph,
        placeholder="The answer was too vague / didn't address X / missed Y...",
        required=True,
        max_length=500
    )
    
    what_would_help = discord.ui.TextInput(
        label="What would have been more helpful?",
        style=discord.TextStyle.paragraph,
        placeholder="I needed specific numbers / more detail on / different approach...",
        required=False,
        max_length=500
    )
    
    def __init__(self, question: str, answer: str):
        super().__init__()
        self.question = question
        self.answer = answer
    
    async def on_submit(self, interaction: discord.Interaction):
        """Process improvement feedback"""
        
        # Record detailed feedback
        bot = interaction.client
        doc_author = bot.get_cog("DocumentAuthor")
        
        if doc_author:
            context = f"""**Original Question:** {self.question}

**What was wrong:** {self.what_was_wrong.value}

**What would help:** {self.what_would_help.value or 'Not specified'}

**Original Answer (truncated):** {self.answer[:300]}..."""
            
            # Create improvement memo immediately
            await doc_author.auto_create_improvement_memo(
                topic=f"User Feedback: {self.question[:60]}",
                question=self.question,
                answer=self.answer,
                feedback_count=1,
                detailed_feedback=context
            )
        
        await interaction.response.send_message(
            "Thanks for the detailed feedback! I'll work on improving this area. 🙏",
            ephemeral=True
        )


async def setup(bot):
    """Setup function for Discord.py - register persistent views"""
    # Register persistent view so buttons work after bot restart
    # We create a "skeleton" view that can handle interactions without the original context
    bot.add_view(PersistentFeedbackView(bot))
    logger.info("Registered persistent FeedbackView for bot restarts")