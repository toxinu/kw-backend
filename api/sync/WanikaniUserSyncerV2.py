import logging

from django.contrib.auth.models import User
from django.utils import timezone
from wanikani_api.client import Client as WkV2Client
from wanikani_api.exceptions import InvalidWanikaniApiKeyException

from kw_webapp.models import Vocabulary, UserSpecific


class WanikaniUserSyncerV2:
    def __init__(self, profile):
        self.logger = logging.getLogger(__name__)
        self.profile = profile
        self.user = self.profile.user
        self.client = WkV2Client(profile.api_key_v2)

    def sync_with_wk(self, full_sync=False):
        """
        Takes a user. Checks the vocab list from WK for all levels. If anything new has been unlocked on the WK side,
        it also unlocks it here on Kaniwani and creates a new review for the user.

        :param user_id: id of the user to sync
        :param full_sync:
        :return: None
        """
        # We split this into two seperate API calls as we do not necessarily know the current level until
        # For the love of god don't delete this next line
        user = User.objects.get(pk=self.user.id)
        self.logger.info("About to begin sync for user {}.".format(user.username))
        profile_sync_succeeded = self.sync_user_profile_with_wk()
        if profile_sync_succeeded:
            if not full_sync:
                new_review_count = self.sync_recent_unlocked_vocab_with_wk_v2()
            else:
                new_review_count = self.sync_unlocked_vocab_with_wk_v2()

            return profile_sync_succeeded, new_review_count
        else:
            self.logger.warning(
                "Not attempting to sync, since API key is invalid, or user has indicated they do not want to be "
                "followed "
            )
            return profile_sync_succeeded, 0, 0

    def sync_user_profile_with_wk(self):
        """
        Hits the WK api with user information in order to synchronize user metadata such as level and gravatar
        information.

        :param user: The user to sync their profile with WK.
        :return: boolean indicating the success of the API call.
        """
        try:
            profile_info = self.client.user_information()
        except InvalidWanikaniApiKeyException:
            self.user.profile.api_valid = False
            self.user.profile.save()
            return False

        self.user.profile.join_date = profile_info.started_at
        self.user.profile.last_wanikani_sync_date = timezone.now()
        self.user.profile.api_valid = True

        if self.user.profile.follow_me:
            self.user.profile.unlocked_levels.get_or_create(level=profile_info.level)
            self.user.profile.handle_wanikani_level_change(profile_info.level)

        self.user.profile.save()

        self.logger.info("Synced {}'s Profile.".format(self.user.username))
        return True

    def sync_recent_unlocked_vocab_with_wk_v2(self):
        if self.user.profile.unlocked_levels_list():
            levels = [
                level
                for level in range(self.user.profile.level - 2, self.profile.level + 1)
                if level in self.user.profile.unlocked_levels_list()
            ]
            if levels:
                try:
                    client = WkV2Client(self.user.profile.api_key_v2)
                    assignments = client.assignments(subject_types="vocabulary", levels=levels, fetch_all=True)
                    new_review_count = self.process_vocabulary_response_for_user_v2(
                        assignments
                    )
                    return new_review_count
                except InvalidWanikaniApiKeyException:
                    self.user.profile.api_valid = False
                    self.user.profile.save()
                except Exception as e:
                    self.logger.warning(
                        "Couldn't sync recent vocab for {}".format(self.user.username), e
                    )
        return 0, 0

    def process_vocabulary_response_for_user_v2(self, assignments):
        """
        Given a response object from Requests.get(), iterate over the list of vocabulary, and synchronize the user.
        :param json_data:
        :param user:
        :return:
        """
        new_review_count = 0
        new_synonym_count = 0
        # Filter items the user has not unlocked.

        for assignment in assignments:
            if self.profile.follow_me:
                review, created = self.process_single_item_from_wanikani_v2(assignment)
                if created:
                    new_review_count += 1
                review.save()
            else:  # User does not want to be followed,just sync synonyms
                self.update_synonyms_for_assignments(assignments)
        self.logger.info("Synced Vocabulary for {}".format(self.user.username))

        return new_review_count

    def process_single_item_from_wanikani_v2(self, assignment):
        try:
            vocab = Vocabulary.objects.get(wk_subject_id=assignment.subject_id)
        except Vocabulary.DoesNotExist:
            self.logger.error(f"Attempted to add a UserSpecific for subject ID: {assignment.subject_id} but failed as we don't have it.")
            return None, False
        review, created = self.associate_vocab_to_user(vocab)
        # TODO IMPLEMENT out_of_date for UserSpecific
        if review.out_of_date(assignment):
            review.reconcile(assignment)
        return review, created #Note that synonym added count will need to be fixed.


    def update_synonyms_for_assignments(self, assignments):
        client = WkV2Client(self.profile.api_key_v2)
        assignment_subject_ids = [assignment.subject_id for assignment in assignments]
        study_materials = client.study_materials(subject_ids=assignment_subject_ids)
        for study_material in study_materials:
            review = UserSpecific.objects.filter(vocabulary__wk_subject_id=study_material.subject_id)
            if review.is_study_material_out_of_date(study_material):
                review.reconcile_study_material(study_material)

    def associate_vocab_to_user(self, vocab):
        """
        takes a vocab, and creates a UserSpecific object for the user based on it. Returns the vocab object.
        :param vocab: the vocabulary object to associate to the user.
        :param user: The user.
        :return: the vocabulary object after association to the user
        """
        try:
            review, created = UserSpecific.objects.get_or_create(
                vocabulary=vocab, user=self.user
            )
            if created:
                review.needs_review = True
                review.next_review_date = timezone.now()
                review.save()
            return review, created

        except UserSpecific.MultipleObjectsReturned:
            us = UserSpecific.objects.filter(vocabulary=vocab, user=self.user)
            for u in us:
                self.logger.error(
                    "during {}'s WK sync, we received multiple UserSpecific objects. Details: {}".format(
                        self.user.username, u
                    )
                )
            return None, None

    def sync_unlocked_vocab_with_wk_v2(self):
        if self.profile.unlocked_levels_list():
            new_review_count = 0

            self.logger.info(
                "Creating sync string for user {}: {}".format(
                    self.user.username, self.profile.api_key_v2
                )
            )
            try:
                assignments = self.client.assignments(subject_types="vocabulary", fetch_all=True)

                new_review_count = self.process_vocabulary_response_for_user_v2(assignments)
                new_review_count += new_review_count
            except InvalidWanikaniApiKeyException:
                self.profile.api_valid = False
                self.profile.save()

            return new_review_count
        else:
            return 0
