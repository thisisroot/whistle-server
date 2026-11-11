from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """Contributors and compat reporters (GitHub/GitLab OAuth).

    Installing apps never needs an account. Defined up front because swapping
    the user model after the first migration is painful.
    """
