# Multi-arch build definition for the CITracker image.
#
# `docker buildx bake` builds every platform of the default target and (with
# --push) publishes one manifest list. The platform list mirrors
# python:3.15-slim exactly -- amd64, arm64, armv7, armv5, 386, ppc64le,
# riscv64, s390x -- because that is what the base image supports.
#
# The default target covers the two production platforms; build the full set
# with `docker buildx bake github-actions-tracker-all` (the exotic ones compile under
# emulation and take a while).

variable "TAG" {
  default = "github-actions-tracker:local"
}

variable "REGISTRY" {
  default = ""
}

group "default" {
  targets = ["github-actions-tracker"]
}

function "tag" {
  params = [name]
  result = REGISTRY == "" ? "${name}:${TAG}" : "${REGISTRY}/${name}:${TAG}"
}

target "github-actions-tracker" {
  dockerfile = "Dockerfile"
  platforms  = ["linux/amd64", "linux/arm64/v8"]
  tags       = [tag("github-actions-tracker")]
}

target "github-actions-tracker-all" {
  inherits = ["github-actions-tracker"]
  platforms = [
    "linux/amd64",
    "linux/arm64/v8",
    "linux/arm/v7",
    "linux/arm/v5",
    "linux/386",
    "linux/ppc64le",
    "linux/riscv64",
    "linux/s390x",
  ]
}
