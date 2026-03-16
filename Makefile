# Makefile for project build automation
# Copyright (c) 2026 Johannes Krohn <jkrohn@cisco.com>
# License: MIT

.PHONY: build publish

build:
	rm -rf dist
	uv build --no-sources

publish:
	uv publish