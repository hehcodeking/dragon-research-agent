.PHONY: demo test architecture serve clean

demo:
	./run.command demo

test:
	python3 -m unittest discover -s tests -v

architecture:
	./run.command agent-architecture

serve:
	./run.command serve --directory reports --port 8765

clean:
	rm -rf reports state build dist *.egg-info dragon_agent.egg-info
