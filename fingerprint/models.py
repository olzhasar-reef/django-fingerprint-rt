from __future__ import annotations
from collections import Counter

from itertools import chain

from django.core.cache import cache
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractBaseUser
from django.contrib.sessions.models import Session
from django.db import models, transaction
from django.db.models import Count
from django.db.models.signals import post_save
from django.dispatch import receiver


class UserSession(models.Model):
    # by default, django stores session data in database; however, we cannot rely on it,
    # since other session backends may be used, plus django doesn't store user-session
    # mapping, so we need to have additional table for that

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, blank=True, null=True, related_name='sessions')
    session_key = models.CharField(max_length=40)
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.session_key

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['session_key', 'user'], name='unique_user_session'),
        ]
        indexes = [
            models.Index(fields=['user', '-created']),
        ]

    def get_value_display(self) -> str:
        return self.session_key[:8]


@receiver(post_save, sender=Session)
def connect_user_to_session(sender, instance, created, **kwargs):
    if user_id := instance.get_decoded().get('_auth_user_id'):
        UserSession.objects.update_or_create(
            session_key=instance.session_key,
            defaults=dict(user_id=user_id),
        )


class Url(models.Model):
    value = models.CharField(max_length=255, unique=True)

    def __str__(self) -> str:
        return self.value

    @classmethod
    def from_value(cls, value: str) -> Url:
        cache_key = getattr(settings, 'FINGERPRINT_URL_CACHE_KEY', 'fingerprint_url')

        if not cache_key:
            return cls.objects.get_or_create(value=value)[0]

        cache_key = f'{cache_key}_{value}'

        if url := cache.get(cache_key):
            return url

        url = cls.objects.get_or_create(value=value)[0]
        cache.set(cache_key, url)
        return url


class AbstractFingerprint(models.Model):
    user_session = models.ForeignKey(UserSession, on_delete=models.CASCADE, related_name='%(model_name)ss')
    url = models.ForeignKey(Url, on_delete=models.CASCADE, related_name='%(model_name)ss')
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True
        indexes = [
            models.Index(fields=['user_session', '-created']),
            models.Index(fields=['url', 'user_session']),
        ]

    @property
    def user(self) -> AbstractBaseUser | None:
        return self.user_session.user

    @classmethod
    def get_count_for_urls(cls, urls: list[str]) -> Counter[str]:

        with transaction.atomic():
            existing_urls = Url.objects.filter(value__in=urls)
            non_existing_urls = set(urls) - {url.value for url in existing_urls}
            new_urls = Url.objects.bulk_create(Url(value=value) for value in non_existing_urls) if non_existing_urls else []

        id_to_url = {url_obj.id: url_obj for url_obj in chain(existing_urls, new_urls)}

        # this is SELECT COUNT(*) GROUP BY in django:
        ids_and_hits = (
            cls.objects
            .filter(url__in=id_to_url.keys())
            .values('url')
            .annotate(hits=Count('user_session', distinct=True))
            .order_by('url')
            .values_list('url', 'hits')
        )

        return Counter({id_to_url[id_].value: hits for id_, hits in ids_and_hits})

    @classmethod
    def get_count_for_objects(cls, request, objects: list[models.Model]) -> Counter[models.Model]:
        url_to_object = {
            request.build_absolute_uri(object.get_absolute_url()): object
            for object in objects
        }
        counter = RequestFingerprint.get_count_for_urls(url_to_object.keys())
        return Counter({object: counter[url] for url, object in url_to_object.items()})


class BrowserFingerprint(AbstractFingerprint):
    visitor_id = models.CharField(max_length=255)

    def __str__(self) -> str:
        return self.visitor_id

    class Meta(AbstractFingerprint.Meta):
        indexes = [
            *AbstractFingerprint.Meta.indexes,
            models.Index(fields=['visitor_id', '-created']),
        ]

    def get_value_display(self) -> str:
        return self.visitor_id[:8]


class RequestFingerprint(AbstractFingerprint):
    ip = models.GenericIPAddressField(blank=True, null=True)
    user_agent = models.CharField(max_length=255, blank=True)
    accept = models.CharField(max_length=255, blank=True)
    content_encoding = models.CharField(max_length=255, blank=True)
    content_language = models.CharField(max_length=255, blank=True)
    referer = models.CharField(max_length=255, blank=True)
    cf_ipcountry = models.CharField(max_length=2, blank=True)

    def __str__(self):
        return f'{self.ip} {self.user_agent}'

    class Meta(AbstractFingerprint.Meta):
        indexes = [
            *AbstractFingerprint.Meta.indexes,
            models.Index(fields=['ip', '-created']),
            models.Index(fields=['user_agent', '-created']),
        ]

    def get_value_display(self) -> str:
        return self.user_agent[:24] + '...'


class UserFingerprint(get_user_model()):
    """ This is just a proxy model for admin site, since django doesn't allow to register two admins for the same model. """
    class Meta:
        proxy = True
