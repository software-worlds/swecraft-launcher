# swecraft-common

Small host-side mechanisms shared by multiple SWECraft services: isolated Git
execution, a locked bare-mirror Git cache, authenticated Forgejo transport, and
the reader-side run-ID grammar. Service policy and domain workflows do not
belong here; seeding, scheduling, evaluation, publishing, and snapshot
orchestration stay with the services that own them.
