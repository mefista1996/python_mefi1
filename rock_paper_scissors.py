import random

rock = '''
    _______
---'   ____)
      (_____)
      (_____)
      (____)
---.__(___)
'''

paper = '''
    _______
---'   ____)____
          ______)
          _______)
         _______)
---.__________)
'''

scissors = '''
    _______
---'   ____)____
          ______)
       __________)
      (____)
---.__(___)
'''

#Write your code below this line 👇

user_input = int(input("What would do you cast? Type 0 for rock, 1 for paper, 2 for scissors\n"))
user_choice = None

if user_input == 0:
    user_choice = rock
elif user_input == 1:
    user_choice = paper
elif user_input == 2:
    user_choice = scissors
else:
    pass

computer_choices = [rock, paper, scissors]
computer_answer = random.choice(computer_choices)

if user_choice == rock and computer_answer == scissors:
    print(f"Your choice was: \n{user_choice}\n Computer choice was: {computer_answer}\nYou won!")
elif computer_answer == rock and user_choice == scissors:
    print(f"Your choice was: \n{user_choice}\n Computer choice was: {computer_answer}\nYou lost!")

elif user_choice == scissors and computer_answer == paper:
    print(f"Your choice was: \n{user_choice}\n Computer choice was: {computer_answer}\nYou won!")
elif computer_answer == scissors and user_choice == paper:
    print(f"Your choice was: \n{user_choice}\n Computer choice was: {computer_answer}\nYou lost!")

elif user_choice == paper and computer_answer == rock:
    print(f"Your choice was: \n{user_choice}\n Computer choice was: {computer_answer}\nYou won!")
elif computer_answer == paper and user_choice == rock:
    print(f"Your choice was: \n{user_choice}\n Computer choice was: {computer_answer}\nYou lost!")
elif computer_answer == user_choice:
    print(f"Your choice was: \n{user_choice}\n Computer choice was: {computer_answer}\nIt's a fair parity!")
else:
    print("Possible incorrect entry - please check your cast")

# another solution
# import random
#
# rock = '''
#     _______
# ---'   ____)
#       (_____)
#       (_____)
#       (____)
# ---.__(___)
# '''
#
# paper = '''
#     _______
# ---'   ____)____
#           ______)
#           _______)
#          _______)
# ---.__________)
# '''
#
# scissors = '''
#     _______
# ---'   ____)____
#           ______)
#        __________)
#       (____)
# ---.__(___)
# '''
#
# game_images = [rock, paper, scissors]
#
# user_choice = int(input("What do you choose? Type 0 for Rock, 1 for Paper or 2 for Scissors.\n"))
# print(game_images[user_choice])
#
# computer_choice = random.randint(0, 2)
# print("Computer chose:")
# print(game_images[computer_choice])
#
# if user_choice >= 3 or user_choice < 0:
#   print("You typed an invalid number, you lose!")
# elif user_choice == 0 and computer_choice == 2:
#   print("You win!")
# elif computer_choice == 0 and user_choice == 2:
#   print("You lose")
# elif computer_choice > user_choice:
#   print("You lose")
# elif user_choice > computer_choice:
#   print("You win!")
# elif computer_choice == user_choice:
#   print("It's a draw")