from random_word import RandomWords
r = RandomWords()
stages = ['''
  +---+
  |   |
  O   |
 /|\  |
 / \  |
      |
=========
''', '''
  +---+
  |   |
  O   |
 /|\  |
 /    |
      |
=========
''', '''
  +---+
  |   |
  O   |
 /|\  |
      |
      |
=========
''', '''
  +---+
  |   |
  O   |
 /|   |
      |
      |
=========''', '''
  +---+
  |   |
  O   |
  |   |
      |
      |
=========
''', '''
  +---+
  |   |
  O   |
      |
      |
      |
=========
''', '''
  +---+
  |   |
      |
      |
      |
      |
=========
''']

word_to_guess = r.get_random_word()
hidden_word = []
for _ in range(len(word_to_guess)):
    hidden_word += "_"
end_of_game = False
print(word_to_guess)

lives = 6

while end_of_game == False:
    user_letter = input("Please guess a letter: ").lower()

    for letter_position in range(len(word_to_guess)):
        letter = word_to_guess[letter_position]
        if letter == user_letter:
            hidden_word[letter_position] = letter

    if user_letter not in word_to_guess:
        lives = lives - 1
        if lives == 0:
            end_of_game = True
            print("You lose")
            print(f"The word was: {word_to_guess}")
    print(f"{' '.join(hidden_word)}")

    if "_" not in hidden_word:
        end_of_game = True
        print("You win")
    print(stages[lives])




