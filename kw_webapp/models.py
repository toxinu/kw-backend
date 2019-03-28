import logging
from itertools import chain

from datetime import timedelta

from django.contrib.postgres.fields import JSONField
from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.contrib.auth.models import User
from django.db.models import Count, F
from django.utils import timezone

from kw_webapp import constants
from kw_webapp.constants import (
    TWITTER_USERNAME_REGEX,
    HTTP_S_REGEX,
    WkSrsLevel,
    WANIKANI_SRS_LEVELS,
)
from kw_webapp.tasks import all_srs

logger = logging.getLogger(__name__)


class Announcement(models.Model):
    title = models.CharField(max_length=255)
    body = models.TextField()
    pub_date = models.DateTimeField("Date Published", auto_now_add=True, null=True)
    creator = models.ForeignKey(User)

    def __str__(self):
        return self.title


class FrequentlyAskedQuestion(models.Model):
    question = models.CharField(max_length=10000)
    answer = models.CharField(max_length=10000)


class Level(models.Model):
    level = models.PositiveIntegerField(
        validators=[
            MinValueValidator(constants.LEVEL_MIN),
            MaxValueValidator(constants.LEVEL_MAX),
        ]
    )

    def __str__(self):
        return str(self.level)


class Profile(models.Model):
    user = models.OneToOneField(User, related_name="profile", on_delete=models.CASCADE)
    api_key = models.CharField(max_length=255)
    api_key_v2 = models.CharField(max_length=255, null=True)
    api_valid = models.BooleanField(default=True)
    gravatar = models.CharField(max_length=255)
    about = models.CharField(max_length=255, default="")
    website = models.CharField(max_length=255, default="N/A", null=True)
    twitter = models.CharField(max_length=255, default="N/A", null=True)
    topics_count = models.PositiveIntegerField(default=0)
    posts_count = models.PositiveIntegerField(default=0)
    title = models.CharField(max_length=255, default="Turtles", null=True)
    join_date = models.DateField(auto_now_add=True, null=True)
    last_wanikani_sync_date = models.DateTimeField(auto_now_add=True, null=True)
    last_visit = models.DateTimeField(null=True, auto_now_add=True)
    level = models.PositiveIntegerField(
        null=True,
        validators=[
            MinValueValidator(constants.LEVEL_MIN),
            MaxValueValidator(constants.LEVEL_MAX),
        ],
    )
    minimum_wk_srs_level_to_review = models.CharField(
        max_length=20, choices=WkSrsLevel.choices(), default=WkSrsLevel.APPRENTICE.name
    )

    maximum_wk_srs_level_to_review = models.CharField(max_length=20, choices=WkSrsLevel.choices(),
                                                      default=WkSrsLevel.BURNED.name)

    order_reviews_by_level = models.BooleanField(default=False)

    # General user-changeable settings
    unlocked_levels = models.ManyToManyField(Level)
    follow_me = models.BooleanField(default=True)
    show_kanji_svg_stroke_order = models.BooleanField(default=False)
    show_kanji_svg_grid = models.BooleanField(default=True)
    kanji_svg_draw_speed = models.PositiveIntegerField(
        default=8,
        validators=[
            MinValueValidator(constants.MIN_SVG_DRAW_SPEED),
            MaxValueValidator(constants.MAX_SVG_DRAW_SPEED),
        ],
    )

    # On Success/Failure of review
    auto_advance_on_success = models.BooleanField(default=False)
    auto_advance_on_success_delay_milliseconds = models.PositiveIntegerField(
        default=1000
    )
    auto_expand_answer_on_success = models.BooleanField(default=True)
    auto_expand_answer_on_failure = models.BooleanField(default=False)
    info_detail_level_on_success = models.PositiveIntegerField(
        default=1, validators=[MaxValueValidator(constants.MAX_REVIEW_DETAIL_LEVEL)]
    )
    info_detail_level_on_failure = models.PositiveIntegerField(
        default=0, validators=[MaxValueValidator(constants.MAX_REVIEW_DETAIL_LEVEL)]
    )

    # External Site settings
    use_eijiro_pro_link = models.BooleanField(default=False)

    # Vacation Settings
    on_vacation = models.BooleanField(default=False)
    vacation_date = models.DateTimeField(default=None, null=True, blank=True)

    def return_from_vacation(self):
        """
        Called when a user disables vacation mode. A one-time pass through their reviews in order to correct their last_studied_date, and quickly run an SRS run to determine which reviews currently need to be looked at.
        """
        logger.info("{} has returned from vacation!".format(self.user.username))
        if self.vacation_date:
            users_reviews = UserSpecific.objects.filter(user=self.user)
            elapsed_vacation_time = timezone.now() - self.vacation_date
            updated_count = users_reviews.update(
                last_studied=F("last_studied") + elapsed_vacation_time
            )
            users_reviews.update(
                next_review_date=F("next_review_date") + elapsed_vacation_time
            )
            logger.info(
                "brought {} reviews out of hibernation for {}".format(
                    updated_count, self.user.username
                )
            )
            logger.info(
                "User {} has been gone for timedelta: {}".format(
                    self.user.username, str(elapsed_vacation_time)
                )
            )

        self.vacation_date = None
        self.on_vacation = False
        self.save()
        all_srs(self.user)
        #TODO MOVE THIS INTO THE VIEW.

    def get_minimum_wk_srs_threshold_for_review(self):
        minimum_wk_srs = self.minimum_wk_srs_level_to_review
        minimum_streak = WANIKANI_SRS_LEVELS[minimum_wk_srs][0]
        return minimum_streak

    def get_maximum_wk_srs_threshold_for_review(self):
        maximum_wk_srs = self.maximum_wk_srs_level_to_review
        # Get the maximum allowable WK srs level from the list of levels -> names.
        maximum_streak = WANIKANI_SRS_LEVELS[maximum_wk_srs][-1]
        return maximum_streak


    def set_twitter_account(self, twitter_account):
        if not twitter_account:
            return

        if twitter_account.startswith("@") and TWITTER_USERNAME_REGEX.match(
            twitter_account[1:]
        ):
            self.twitter = twitter_account
        elif TWITTER_USERNAME_REGEX.match(twitter_account):
            self.twitter = "@{}".format(twitter_account)
        else:
            logger.warning(
                "WK returned a funky twitter account name: {},  for user:{} ".format(
                    twitter_account, self.user.username
                )
            )

        self.save()

    def set_website(self, website_url):
        if website_url:
            fixed_site = HTTP_S_REGEX.sub("", website_url)
            if fixed_site:
                self.website = fixed_site
                self.save()

    def unlocked_levels_list(self):
        x = self.unlocked_levels.values_list("level")
        x = [x[0] for x in x]
        return x

    def handle_wanikani_level_change(self, new_level):
        self.level = new_level
        self.save()

    def __str__(self):
        return "{} -- {} -- {} -- {}".format(
            self.user.username, self.api_key, self.level, self.unlocked_levels_list()
        )


class PartOfSpeech(models.Model):
    part = models.CharField(max_length=30)

    def __str__(self):
        return str(self.part)


class Vocabulary(models.Model):
    meaning = models.CharField(max_length=255)
    alternate_meanings = models.CharField
    wk_subject_id = models.IntegerField(default=0) #TODO we will need to run a one-time script to match up vocab by kanji, then assign a WK id.
    wk_last_modified = models.DateTimeField(null=True)
    parts_of_speech = models.ManyToManyField(PartOfSpeech)
    auxiliary_meanings_whitelist = models.CharField(max_length=500, null=True)
    level = models.PositiveIntegerField(
        null=True,
        validators=[
            MinValueValidator(constants.LEVEL_MIN),
            MaxValueValidator(constants.LEVEL_MAX),
        ],
    )

    def reading_count(self):
        return self.readings.all().count()

    def get_absolute_url(self):
        return "https://www.wanikani.com/vocabulary/{}/".format(self.readings.all()[0])

    def is_out_of_date(self, vocabulary):
        return self.wk_last_modified is None or vocabulary.data_updated_at > self.wk_last_modified

    def reconcile(self, vocabulary):
        self.wk_last_modified = vocabulary.data_updated_at
        self.level = vocabulary.level
        # Set whatever is the new primary meaning.
        for meaning_obj in vocabulary.meanings:
            if meaning_obj.primary:
                self.meaning = meaning_obj.meaning

        # Reset alternate and auxiliary meanings to whatever is current
        self.alternate_meanings = ",".join([m.meaning for m in vocabulary.meanings if not m.primary])
        self.auxiliary_meanings_whitelist = ",".join([aux.meaning for aux in vocabulary.auxiliary_meanings])

        # Reconcile the difference in readings.
        self._delete_stale_readings_based_on(vocabulary)
        self._add_new_readings_based_on(vocabulary)
        self._reconcile_parts_of_speech_based_on(vocabulary)

        self.save()

    def _reconcile_parts_of_speech_based_on(self, vocabulary):
        self.parts_of_speech.clear()
        for pos in vocabulary.parts_of_speech:
            self.parts_of_speech.get_or_create(part=pos)

    def _delete_stale_readings_based_on(self, vocabulary):
        reading_kanas = [r.reading for r in  vocabulary.readings]
        # Clear out old readings that aren't needed anymore.
        reading_ids_to_delete = []
        for reading in self.readings.all():
            if reading.kana not in reading_kanas:
                reading_ids_to_delete.append(reading.id)
        self.readings.filter(id__in=reading_ids_to_delete).delete()

    def _add_new_readings_based_on(self, vocabulary):
        # Add new readings that weren't there before
        current_reading_kanas = [reading.kana for reading in self.readings.all()]
        readings_to_add = []
        for reading_obj in vocabulary.readings:
            if reading_obj.reading not in current_reading_kanas:
                new_reading = Reading()
                new_reading.vocabulary = self
                new_reading.kana = reading_obj.reading
                new_reading.character = vocabulary.characters
                new_reading.level = vocabulary.level
                readings_to_add.append(new_reading)
        self.readings.add(*readings_to_add, bulk=False)

    def __str__(self):
        return self.meaning


class Tag(models.Model):
    """
    A model meant to handle tagging readings.
    """

    name = models.CharField(max_length=255, unique=True)

    def get_all_vocabulary(self):
        return Vocabulary.objects.filter(readings__tags__id=self.id).distinct()

    def __str__(self):
        return self.name


class Reading(models.Model):
    vocabulary = models.ForeignKey(
        Vocabulary, related_name="readings", on_delete=models.CASCADE
    )
    character = models.CharField(max_length=255)
    kana = models.CharField(max_length=255)
    level = models.PositiveIntegerField(
        null=True,
        validators=[
            MinValueValidator(constants.LEVEL_MIN),
            MaxValueValidator(constants.LEVEL_MAX),
        ],
    )

    # JISHO information
    sentence_en = models.CharField(max_length=1000, null=True)
    sentence_ja = models.CharField(max_length=1000, null=True)
    common = models.NullBooleanField()
    tags = models.ManyToManyField(Tag)
    furigana = models.CharField(max_length=100, null=True)
    pitch = models.CharField(max_length=100, null=True)
    parts_of_speech = models.ManyToManyField(PartOfSpeech)
    furigana_sentence_ja = JSONField(max_length=1000, default={})

    class Meta:
        unique_together = ("character", "kana")

    def __str__(self):
        return "{} - {} - {} - {}".format(
            self.vocabulary.meaning, self.kana, self.character, self.level
        )

wk_last_seen_date = models.DateTimeField()

class Report(models.Model):
    created_by = models.ForeignKey(User)
    created_at = models.DateTimeField(auto_now_add=True)
    reading = models.ForeignKey(
        Reading, on_delete=models.CASCADE, related_name="reports"
    )
    reason = models.CharField(max_length=1000)

    def __str__(self):
        return "Report: reading [{}]: {}, by user [{}] at {}".format(
            self.reading_id, self.reason, self.created_by_id, self.created_at
        )


class LessonManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(streak=0)

class ReviewManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(streak__gte=1)

class UserSpecific(models.Model):
    vocabulary = models.ForeignKey(Vocabulary)
    user = models.ForeignKey(User, related_name="reviews", on_delete=models.CASCADE)
    correct = models.PositiveIntegerField(default=0)
    incorrect = models.PositiveIntegerField(default=0)
    streak = models.PositiveIntegerField(default=0)
    last_studied = models.DateTimeField(blank=True, null=True)
    needs_review = models.BooleanField(default=True)
    unlock_date = models.DateTimeField(default=timezone.now, blank=True)
    next_review_date = models.DateTimeField(default=timezone.now, null=True, blank=True)
    burned = models.BooleanField(default=False)
    hidden = models.BooleanField(default=False)
    wanikani_srs = models.CharField(max_length=255, default="unknown")
    wanikani_srs_numeric = models.IntegerField(default=0)
    wanikani_burned = models.BooleanField(default=False)
    notes = models.CharField(max_length=500, editable=True, blank=True, null=True)
    critical = models.BooleanField(default=False)
    wk_assignment_last_modified = models.DateTimeField(null=True)
    wk_study_materials_last_modified = models.DateTimeField(null=True)
    meaning_note = models.CharField(max_length=2000, null=True)
    reading_note = models.CharField(max_length=2000, null=True)

    lessons = LessonManager()
    reviews = ReviewManager()

    class Meta:
        unique_together = ("vocabulary", "user")

    def is_assignment_out_of_date(self, assignment):
        return self.wk_assignment_last_modified is None or self.wk_assignment_last_modified < assignment.data_updated_at

    def is_study_material_out_of_date(self, study_material):
        return self.wk_study_materials_last_modified is None or self.wk_study_materials_last_modified < study_material.data_updated_at

    def reconcile_assignment(self, assignment):
        self.wanikani_srs = assignment.srs_stage_name
        self.wanikani_srs_numeric = assignment.srs_stage
        self.wanikani_burned = assignment.burned_at is not None
        self.wk_assignment_last_modified = assignment.data_updated_at
        self.save()

    def reconcile_study_material(self, study_material):
        self.meaning_note = study_material.meaning_note
        self.reading_note = study_material.reading_note
        self._add_meaning_synonyms(study_material.meaning_synonyms)
        self.wk_study_materials_last_modified = study_material.data_updated_at
        self.save()

    def _add_meaning_synonyms(self, meaning_synonyms):
        if meaning_synonyms is not None:
            meaning_synonyms_to_add = []
            for meaning_synonym in meaning_synonyms:
                synonym = MeaningSynonym()
                synonym.text = meaning_synonym
                synonym.review = self
                meaning_synonyms_to_add.append(synonym)
            self.meaning_synonyms.all().delete()
            self.meaning_synonyms.add(*meaning_synonyms_to_add, bulk=False)

    def answered_correctly(self, first_try=True):
        # This is a check to see if it is a "lesson" object.
        if self.streak == 0:
            self.streak += 1
        elif first_try:
            self.correct += 1
            self.streak += 1
            if self.streak >= constants.WANIKANI_SRS_LEVELS[WkSrsLevel.BURNED.name][0]:
                self.burned = True

        self.needs_review = False
        self.last_studied = timezone.now()
        self.set_next_review_time()
        self.set_criticality()
        self.save()
        return self

    def answered_incorrectly(self):
        """
        Helper function to correctly decrement streak value and increase count of incorrect.
        If user is nearing burned status, they get doubly-decremented.
        """
        self.incorrect += 1
        # If user is about to burn, drop them two levels.
        if self.streak == 7:
            self.streak -= 2
        # streak of 0 indicates "Lesson" and we don't want users dropping down to lesson.
        elif self.streak > 1:
            self.streak -= 1

        self.streak = max(0, self.streak)
        self.save()
        self.set_criticality()
        return self

    def set_criticality(self):
        if self.is_critical():
            self.critical = True
        else:
            self.critical = False

    def _can_be_critical(self):
        return (
            self.correct + self.incorrect
            >= constants.MINIMUM_ATTEMPT_COUNT_FOR_CRITICALITY
        )

    def _breaks_threshold(self):
        return (
            float(self.incorrect) / float(self.correct + self.incorrect)
            >= constants.CRITICALITY_THRESHOLD
        )

    def is_critical(self):
        if self._can_be_critical() and self._breaks_threshold():
            return True
        else:
            return False

    def get_all_readings(self):
        return list(chain(self.vocabulary.readings.all(), self.reading_synonyms.all()))

    def can_be_managed_by(self, user):
        return self.user == user or user.is_superuser

    def synonyms_list(self):
        return [synonym.text for synonym in self.meaning_synonyms.all()]

    def synonyms_string(self):
        return ", ".join([synonym.text for synonym in self.meaning_synonyms.all()])

    def remove_synonym(self, text):
        MeaningSynonym.objects.get(text=text).delete()

    def reading_synonyms_list(self):
        return [synonym.kana for synonym in self.reading_synonyms.all()]

    def add_answer_synonym(self, kana, character):
        synonym, created = self.reading_synonyms.get_or_create(
            kana=kana, character=character
        )
        return synonym, created

    def add_meaning_synonym(self, text):
        synonym, created = self.meaning_synonyms.get_or_create(text=text)
        return synonym, created

    def set_next_review_time(self):
        if self.streak not in constants.SRS_TIMES.keys():
            self.next_review_date = None
        else:
            self.next_review_date = timezone.now() + timedelta(
                hours=constants.SRS_TIMES[self.streak]
            )
            self._round_next_review_date()
        self.save()

    def set_next_review_time_based_on_last_studied(self):
        self.next_review_date = self.last_studied + timedelta(
            hours=constants.SRS_TIMES[self.streak]
        )
        self._round_review_time_up()
        self.save()

    def bring_review_out_of_vacation(self, vacation_duration):
        self.last_studied = self.last_studied + vacation_duration
        if self.streak in constants.SRS_TIMES.keys():
            self.next_review_date = self.last_studied + timezone.timedelta(
                hours=constants.SRS_TIMES[self.streak]
            )
            self.round_times()
        else:
            self.next_review_date = None

        self.save()

    def round_times(self):
        if self.streak in constants.SRS_TIMES.keys():
            self._round_review_time_up()
            self._round_last_studied_up()

    def reset(self):
        # During a reset, we bring them down to the lowest review level, _not_ lesson level.
        self.streak = 1
        self.last_studied = None
        self.next_review_date = timezone.now()
        self.correct = 1
        self.incorrect = 0
        self.burned = False
        self.needs_review = True
        self.save()

    def _round_last_studied_up(self):
        original_date = self.last_studied
        round_to = constants.REVIEW_ROUNDING_TIME.total_seconds()
        seconds = (
            self.last_studied
            - self.last_studied.min.replace(tzinfo=self.last_studied.tzinfo)
        ).seconds
        rounding = (seconds + round_to) // round_to * round_to
        self.last_studied = self.last_studied + timedelta(0, rounding - seconds, 0)

        logger.debug(
            "Updating Last Studied Time for user {} for review {}. Went from {} to {}, a rounding of {:.1f} minutes".format(
                self.user,
                self.vocabulary.meaning,
                original_date.strftime("%H:%M:%S"),
                self.last_studied.strftime("%H:%M:%S"),
                (self.last_studied - original_date).total_seconds() / 60,
            )
        )
        self.save()

    def _round_next_review_date(self):
        round_to = constants.REVIEW_ROUNDING_TIME.total_seconds()
        seconds = (
            self.next_review_date
            - self.next_review_date.min.replace(tzinfo=self.next_review_date.tzinfo)
        ).seconds
        rounding = (seconds + round_to) // round_to * round_to
        self.next_review_date = self.next_review_date + timedelta(
            0, rounding - seconds, 0
        )
        self.save()

    def _round_last_studied_date(self):
        round_to = constants.REVIEW_ROUNDING_TIME.total_seconds()
        seconds = (
            self.last_studied
            - self.last_studied.min.replace(tzinfo=self.last_studied.tzinfo)
        ).seconds
        rounding = (seconds + round_to) // round_to * round_to
        self.last_studied = self.last_studied + timedelta(0, rounding - seconds, 0)
        self.save()

    def _round_review_time_up(self):
        self._round_next_review_date()
        self._round_last_studied_date()

    def __str__(self):
        return "{} - {} - {} - c:{} - i:{} - s:{} - ls:{} - nr:{} - uld:{}".format(
            self.id,
            self.vocabulary.meaning,
            self.user.username,
            self.correct,
            self.incorrect,
            self.streak,
            self.last_studied,
            self.needs_review,
            self.unlock_date,
        )


class AnswerSynonym(models.Model):
    character = models.CharField(max_length=255, null=True)
    kana = models.CharField(max_length=255, null=False)
    review = models.ForeignKey(UserSpecific, related_name="reading_synonyms", null=True)

    class Meta:
        unique_together = ("character", "kana", "review")

    def __str__(self):
        return "{} - {} - {} - SYNONYM".format(
            self.review.vocabulary.meaning, self.kana, self.character
        )

    def as_dict(self):
        return {
            "id": self.id,
            "kana": self.kana,
            "character": self.character,
            "review_id": self.review.id,
        }


class MeaningSynonym(models.Model):
    text = models.CharField(max_length=255, blank=False, null=False)
    review = models.ForeignKey(UserSpecific, related_name="meaning_synonyms", null=True)

    def __str__(self):
        return self.text

    class Meta:
        unique_together = ("text", "review")
