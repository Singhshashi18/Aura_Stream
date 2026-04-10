import uuid

from django.db import models

class AuraSession(models.Model):
    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    model_type = models.CharField(max_length=32)
    started_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.uuid} ({self.model_type})"


class ThoughtLog(models.Model):
    session = models.ForeignKey(AuraSession, on_delete=models.CASCADE, related_name="thought_logs")
    thought_block = models.TextField()
    final_response = models.TextField()
    interrupted_by = models.TextField(blank=True, default="", help_text="Partial response that was interrupted (if applicable)")
    interruption_type = models.CharField(max_length=32, blank=True, default="", choices=[
        ("user_speech", "User speech detected"),
        ("background_noise", "Background noise (ignored)"),
        ("not_interrupted", "Response completed normally"),
    ])
    resumption_context = models.TextField(blank=True, default="", help_text="How next response pivoted from interruption")

    def __str__(self):
        return f"ThoughtLog:{self.id} for {self.session.uuid}"


class AudioArtifact(models.Model):
    session = models.ForeignKey(AuraSession, on_delete=models.CASCADE, related_name="audio_artifacts")
    file_path = models.CharField(max_length=512)
    duration = models.FloatField(help_text="Audio length in seconds")

    def __str__(self):
        return f"AudioArtifact:{self.id} ({self.duration}s)"


class AgentActivity(models.Model):
    session = models.ForeignKey(AuraSession, on_delete=models.CASCADE, related_name="agent_activities")
    tool_called = models.CharField(max_length=128)
    result = models.JSONField()

    def __str__(self):
        return f"AgentActivity:{self.tool_called} for {self.session.uuid}"
